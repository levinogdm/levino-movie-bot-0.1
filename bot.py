import logging
import asyncio
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import TelegramError

import config
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# temporary buffer while collecting album items posted to the private channel
media_groups: dict[str, list] = {}


# ---------------- Helper functions ----------------

async def is_joined(bot, user_id: int) -> bool:
    for channel_id in config.FORCE_SUB_CHANNEL_IDS:
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status not in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                return False
        except TelegramError as e:
            logger.warning(f"get_chat_member failed for {channel_id}: {e}")
            return False
    return True


def join_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=config.MAIN_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I've Joined", callback_data=f"check_{code}")],
    ])


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        logger.info(f"Could not delete message {message_id} in {chat_id}: {e}")


async def deliver_file(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE):
    record = db.get_file(code)
    if not record:
        await context.bot.send_message(chat_id, f"⚠️ File kandilla / link expire aayi.\n\n{config.FOOTER}")
        return

    caption = (
        f"🎬 {record['title']}\n\n"
        f"⏳ Ee file {config.AUTO_DELETE_SECONDS // 60} minute kazhinjal automatic aayi delete aavum.\n\n"
        f"{config.FOOTER}"
    )

    if record["file_type"] == "video":
        sent = await context.bot.send_video(chat_id, record["file_id"], caption=caption)
    elif record["file_type"] == "document":
        sent = await context.bot.send_document(chat_id, record["file_id"], caption=caption)
    else:
        sent = await context.bot.send_photo(chat_id, record["file_id"], caption=caption)

    context.job_queue.run_once(
        delete_message_job, config.AUTO_DELETE_SECONDS, data=(chat_id, sent.message_id)
    )


async def log_search(context: ContextTypes.DEFAULT_TYPE, user, query_text: str, found: bool):
    """Send a log entry to the LOG_CHANNEL_ID every time a user searches for something."""
    status = "✅ Found" if found else "❌ Not found"
    username = f"@{user.username}" if user.username else "—"
    text = (
        f"🔍 <b>Search Log</b>\n\n"
        f"👤 Name: {user.first_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"📝 Query: {query_text}\n"
        f"📊 Status: {status}"
    )
    try:
        await context.bot.send_message(
            config.LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        logger.warning(f"Failed to log to log channel: {e}")


# ---------------- Private channel: capture admin uploads ----------------
# Admin workflow: send the poster PHOTO + the movie VIDEO/DOCUMENT together as one
# album (select both in Telegram and send at once) to the PRIVATE channel.
# Put the movie name in the photo's caption. The bot links them automatically.
# (If you only send a video/document with a caption and no separate photo, that
# file itself is used as the preview in the main channel.)

async def process_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(2)  # give Telegram time to deliver every item in the album
    messages = media_groups.pop(media_group_id, [])
    if messages:
        await handle_collected_messages(messages, context)


# Removes URLs, @mentions and t.me links, and only keeps the FIRST LINE of
# whatever caption the admin sent. This means any promo text, join-channel
# instructions, or extra lines added below the movie name are dropped
# completely — only the plain movie name ever reaches the main channel post.
URL_PATTERN = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+|@\w+)", re.IGNORECASE)
EMOJI_PREFIX_PATTERN = re.compile(r"^[\W_]+", re.UNICODE)  # strip leading emoji/symbols like 🎬


def extract_title(raw_caption: str) -> str:
    if not raw_caption:
        return "Untitled"

    first_line = next((line.strip() for line in raw_caption.splitlines() if line.strip()), "")
    if not first_line:
        return "Untitled"

    cleaned = URL_PATTERN.sub("", first_line)
    cleaned = EMOJI_PREFIX_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|•\n\t")
    return cleaned or "Untitled"


NEW_ARRIVAL_TEXT = "🆕 Latest movie collection vannittundu!"


async def notify_requesters(title: str, code: str, context: ContextTypes.DEFAULT_TYPE):
    """After a new file is saved, check if anyone had requested a matching
    title and DM them directly, then mark their request as fulfilled."""
    matches = db.get_matching_requests(title)
    for req in matches:
        button = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Get Movie", url=f"https://t.me/{config.BOT_USERNAME}?start={code}")
        ]])
        try:
            await context.bot.send_message(
                req["user_id"],
                f"🎉 Ningal request cheytha movie ready aayi!\n\n"
                f"🎬 <b>{title}</b>\n\n{config.FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=button,
            )
        except TelegramError as e:
            logger.info(f"Could not DM requester {req['user_id']}: {e}")
        db.mark_request_fulfilled(req["id"])


async def handle_collected_messages(messages: list, context: ContextTypes.DEFAULT_TYPE):
    poster_file_id = None
    thumb_file_id = None  # fallback: video/document's own auto-generated preview image
    file_id = None
    file_type = None
    raw_caption = None

    for msg in messages:
        if msg.caption:
            raw_caption = msg.caption.strip()
        if msg.photo:
            poster_file_id = msg.photo[-1].file_id
        elif msg.video:
            file_id = msg.video.file_id
            file_type = "video"
            if msg.video.thumbnail:
                thumb_file_id = msg.video.thumbnail.file_id
        elif msg.document:
            file_id = msg.document.file_id
            file_type = "document"
            if msg.document.thumbnail:
                thumb_file_id = msg.document.thumbnail.file_id

    if not file_id:
        return  # only a poster arrived, nothing to link yet

    title = extract_title(raw_caption)
    code = db.save_file(file_id, file_type, title)

    # Caption is built fresh here from ONLY the clean title — it never
    # carries over the original message's promo text, links, mentions, or
    # forward info. Only this + the single "Get Movie" button will appear
    # on the main channel post.
    caption = f"🎬 <b>{title}</b>\n\n{NEW_ARRIVAL_TEXT}\n{config.FOOTER}"
    button = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Get Movie", url=f"https://t.me/{config.BOT_USERNAME}?start={code}")
    ]])

    # IMPORTANT: the main channel post must NEVER contain the actual video
    # or document — that would let people download the file directly from
    # the channel, skipping the bot and the force-subscribe check entirely.
    # The real file is only ever sent by the bot, in DM, after join is
    # verified (see deliver_file). Here we only ever send an IMAGE
    # (the admin's poster photo, or the file's own auto-thumbnail) or,
    # if neither exists, plain text — never send_video/send_document.
    try:
        if poster_file_id:
            # A real poster photo's file_id can be reused directly.
            await context.bot.send_photo(
                config.MAIN_CHANNEL_ID, poster_file_id, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
        elif thumb_file_id:
            # Telegram does NOT allow a video/document's "thumbnail" file_id
            # to be reused directly as a photo (different internal file
            # type), so we download the thumbnail's bytes first and then
            # upload it fresh as a brand-new photo.
            tg_file = await context.bot.get_file(thumb_file_id)
            photo_bytes = await tg_file.download_as_bytearray()
            await context.bot.send_photo(
                config.MAIN_CHANNEL_ID, bytes(photo_bytes), caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
        else:
            await context.bot.send_message(
                config.MAIN_CHANNEL_ID, caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
    except TelegramError as e:
        logger.error(f"Failed to post to main channel: {e}")

    # Notify anyone who had requested a matching title, now that it's live.
    await notify_requesters(title, code, context)


async def private_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None:
        return

    if msg.media_group_id:
        group = media_groups.setdefault(msg.media_group_id, [])
        group.append(msg)
        if len(group) == 1:
            context.application.create_task(process_group(msg.media_group_id, context))
    else:
        await handle_collected_messages([msg], context)


# ---------------- User commands ----------------

WELCOME_TEXT = (
    "Hai! 🎬 Movie links ee bot vazhi share cheyyunnu.\n"
    "Main channel-il oru movie post kanumbol, athinte 'Get Movie' button click cheyyuka.\n\n"
    f"{config.FOOTER}"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)

    if not context.args:
        await update.message.reply_text(WELCOME_TEXT)
        return

    code = context.args[0]

    if not await is_joined(context.bot, user.id):
        await update.message.reply_text(
            f"⚠️ Ee movie edukkan main channel join cheyyanam.\n\n{config.FOOTER}",
            reply_markup=join_keyboard(code),
        )
        return

    await deliver_file(update.effective_chat.id, code, context)


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    code = query.data.split("check_", 1)[1]
    user_id = query.from_user.id

    if await is_joined(context.bot, user_id):
        await query.answer("✅ Confirm cheythu!")
        await query.message.delete()
        await deliver_file(query.message.chat.id, code, context)
    else:
        await query.answer("❌ Ninnal ippozhum channel join cheythittilla!", show_alert=True)


# ---------------- Search by movie name (DM only) ----------------
# User types a movie name directly to the bot in DM. The bot searches all
# titles ever posted from the private channel and, if the user has joined
# the main channel, delivers the matching file the same way deliver_file
# always does (fresh from the bot, auto-deletes after AUTO_DELETE_SECONDS).
# Every search (whatever text is typed, movie or not) is also logged to
# LOG_CHANNEL_ID via log_search(). When nothing matches, the user gets a
# "Request this movie" button (see request_callback below).

async def search_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        return

    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)

    results = db.search_files(query_text)
    await log_search(context, user, query_text, found=bool(results))

    if not results:
        req_id = db.create_request(user.id, query_text)
        await update.message.reply_text(
            f"😔 '{query_text}' ennu match aavunna movie kandilla.\n\n"
            f"Ee movie venamenkil request cheyyam, kittiyal njan DM ayakkam.\n\n{config.FOOTER}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Request this movie", callback_data=f"reqconfirm_{req_id}")
            ]]),
        )
        return

    if not await is_joined(context.bot, user.id):
        if len(results) == 1:
            # Single match — reuse the same join-then-deliver flow as /start deep links.
            await update.message.reply_text(
                f"⚠️ Ee movie edukkan main channel join cheyyanam.\n\n{config.FOOTER}",
                reply_markup=join_keyboard(results[0]["code"]),
            )
        else:
            await update.message.reply_text(
                "⚠️ Movies edukkan main channel join cheyyanam. "
                f"Join cheythu shesham veendum movie peru type cheyyuka.\n\n{config.FOOTER}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📢 Join Channel", url=config.MAIN_CHANNEL_LINK)
                ]]),
            )
        return

    if len(results) == 1:
        await deliver_file(update.effective_chat.id, results[0]["code"], context)
        return

    buttons = [
        [InlineKeyboardButton(f"🎬 {r['title']}", callback_data=f"getfile_{r['code']}")]
        for r in results
    ]
    await update.message.reply_text(
        f"🔎 '{query_text}' -nu ee movies kittiyi, venda ondu select cheyyuka:\n\n{config.FOOTER}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def getfile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    code = query.data.split("getfile_", 1)[1]
    user_id = query.from_user.id

    if not await is_joined(context.bot, user_id):
        await query.answer("❌ Ninnal ippozhum channel join cheythittilla!", show_alert=True)
        return

    await query.answer()
    await deliver_file(query.message.chat.id, code, context)


async def request_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Request this movie' — flip the request to 'requested' and
    post it to the request channel for the admin to see."""
    query = update.callback_query
    req_id = int(query.data.split("reqconfirm_", 1)[1])

    req = db.get_request(req_id)
    if not req:
        await query.answer("⚠️ Ee request kandilla.", show_alert=True)
        return

    if req["status"] == "requested":
        await query.answer("✅ Already requested cheythathanu!")
        return

    req = db.confirm_request(req_id)
    user = query.from_user
    username = f"@{user.username}" if user.username else "—"

    try:
        await context.bot.send_message(
            config.REQUEST_CHANNEL_ID,
            f"📩 <b>New Movie Request</b>\n\n"
            f"🎬 Movie: {req['query_text']}\n"
            f"👤 Name: {user.first_name}\n"
            f"🔗 Username: {username}\n"
            f"🆔 ID: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning(f"Failed to post request to request channel: {e}")

    await query.answer("✅ Request submit cheythu!")
    await query.edit_message_text(
        f"✅ Request submit cheythu: '{req['query_text']}'\n\n"
        f"Ee movie kittiyal njan automatic ayi DM ayakkam.\n\n{config.FOOTER}"
    )


# ---------------- Admin commands ----------------

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in config.ADMIN_IDS:
            await update.message.reply_text("🚫 Ith admin command aanu.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛠 Admin Panel\n\n"
        "/stats - User & file statistics\n"
        "/requests - Pending movie requests\n"
        "/broadcast - (reply to a message) send it to all users\n\n"
        f"{config.FOOTER}"
    )
    await update.message.reply_text(text)


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats()
    await update.message.reply_text(
        f"👥 Total Users: {stats['users']}\n"
        f"🎬 Total Files: {stats['files']}\n\n"
        f"{config.FOOTER}"
    )


@admin_only
async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = db.get_pending_requests()
    if not pending:
        await update.message.reply_text(f"📭 Pending requests onnum illa.\n\n{config.FOOTER}")
        return

    lines = [f"• {r['query_text']} (user: {r['user_id']})" for r in pending]
    text = "📩 <b>Pending Requests</b>\n\n" + "\n".join(lines) + f"\n\n{config.FOOTER}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Broadcast cheyyende message-nu reply cheythu /broadcast type cheyyuka.")
        return

    source = update.message.reply_to_message
    user_ids = db.get_all_user_ids()
    sent, failed = 0, 0

    status_msg = await update.message.reply_text(f"Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await source.copy(chat_id=uid)
            sent += 1
        except TelegramError:
            failed += 1
            db.remove_user(uid)
        await asyncio.sleep(0.05)  # stay under Telegram's rate limits

    await status_msg.edit_text(
        f"✅ Broadcast complete.\nSent: {sent}\nFailed/removed: {failed}\n\n{config.FOOTER}"
    )


# ---------------- Error handler ----------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)


# ---------------- Main ----------------

def main():
    db.init_db()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("requests", requests_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^check_"))
    app.add_handler(CallbackQueryHandler(getfile_callback, pattern=r"^getfile_"))
    app.add_handler(CallbackQueryHandler(request_confirm_callback, pattern=r"^reqconfirm_"))
    app.add_handler(MessageHandler(filters.Chat(chat_id=config.PRIVATE_CHANNEL_ID), private_channel_handler))
    # Any plain text a user sends the bot in DM (not a command) is treated
    # as a movie name search. Must be added AFTER the private-channel
    # handler above so channel posts are never mistaken for a search.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, search_movie))

    app.add_error_handler(error_handler)

    app.run_webhook(
        listen="0.0.0.0",
        port=config.PORT,
        url_path=config.BOT_TOKEN,
        webhook_url=f"{config.WEBHOOK_URL}/{config.BOT_TOKEN}",
    )


if __name__ == "__main__":
    main()
