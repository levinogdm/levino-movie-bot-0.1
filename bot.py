import logging
import asyncio
import re
import base64

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

# Place a JPG file named exactly this, in the same folder as bot.py, to use
# as the thumbnail shown whenever the bot delivers a video/document to a
# user. Telegram requires it to be <=200KB and <=320x320 pixels.
LOGO_THUMB_PATH = "thumb.jpg"
_cached_logo_bytes = None

# A smaller version (e.g. 100x100px) used only for the quality-selection
# menu message, so it shows as a small compact preview instead of a big photo.
MENU_THUMB_PATH = "menu_thumb.jpg"
_cached_menu_thumb_bytes = None


def get_logo_thumbnail():
    global _cached_logo_bytes
    if _cached_logo_bytes is None:
        try:
            with open(LOGO_THUMB_PATH, "rb") as f:
                _cached_logo_bytes = f.read()
        except FileNotFoundError:
            logger.warning(f"{LOGO_THUMB_PATH} not found — files will be sent without a custom thumbnail.")
            _cached_logo_bytes = b""
    return _cached_logo_bytes or None


def get_menu_thumbnail():
    global _cached_menu_thumb_bytes
    if _cached_menu_thumb_bytes is None:
        try:
            with open(MENU_THUMB_PATH, "rb") as f:
                _cached_menu_thumb_bytes = f.read()
        except FileNotFoundError:
            logger.warning(f"{MENU_THUMB_PATH} not found — falling back to the bigger logo for the menu.")
            _cached_menu_thumb_bytes = get_logo_thumbnail() or b""
    return _cached_menu_thumb_bytes or None


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


def join_keyboard(code: str, title: str = None) -> InlineKeyboardMarkup:
    check_payload = code
    if title:
        # "check_" prefix eats into the 64-byte callback_data budget.
        check_payload = encode_start_payload(code, title, max_len=64 - len("check_"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=config.MAIN_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I've Joined", callback_data=f"check_{check_payload}")],
    ])


def format_size(n):
    if not n:
        return ""
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_ids = context.job.data
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as e:
            logger.info(f"Could not delete message {message_id} in {chat_id}: {e}")


async def deliver_single_file(chat_id: int, title: str, record: dict, context: ContextTypes.DEFAULT_TYPE, poster_file_id: str = None):
    size_text = format_size(record.get("file_size")) or "Unknown size"
    label = record.get("label") or "Download"

    caption = (
        f"🎬 {title}\n"
        f"⚡ {size_text} ▪ {label}\n\n"
        f"⏳ Ee file {config.AUTO_DELETE_SECONDS // 60} minute kazhinjal automatic aayi delete aavum.\n\n"
        f"{config.FOOTER}"
    )

    # Fixed bot-logo thumbnail (see LOGO_THUMB_PATH above), shown on every
    # video/document the bot sends to a user.
    thumb_bytes = get_logo_thumbnail()

    if record["file_type"] == "video":
        sent = await context.bot.send_video(chat_id, record["file_id"], caption=caption, thumbnail=thumb_bytes)
    elif record["file_type"] == "document":
        sent = await context.bot.send_document(chat_id, record["file_id"], caption=caption, thumbnail=thumb_bytes)
    else:
        sent = await context.bot.send_photo(chat_id, record["file_id"], caption=caption)

    context.job_queue.run_once(
        delete_message_job, config.AUTO_DELETE_SECONDS, data=(chat_id, [sent.message_id])
    )


def quality_button_text(record: dict) -> str:
    size_text = format_size(record.get("file_size")) or "Unknown size"
    label = record.get("label") or "Download"
    return f"⚡ {size_text} ▪ {label}"


async def show_quality_menu(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE, user_id: int = None, fallback_title: str = None):
    """If a movie has only one file, deliver it straight away. If it has
    several (different sizes/qualities), show a selection menu instead.

    If the movie/files are missing (e.g. database got wiped on a restart)
    but a fallback_title was recovered from the button's own link — see
    decode_start_payload() — a request is created automatically and the
    user only needs ONE tap to confirm it, no retyping needed."""
    movie = db.get_movie(code)
    files = db.get_movie_files(code) if movie else []

    if not movie or not files:
        if fallback_title and user_id:
            req_id = db.create_request(user_id, fallback_title)
            await context.bot.send_message(
                chat_id,
                f"⚠️ Ee link expire aayi.\n\n"
                f"🎬 <b>{fallback_title}</b>\n\n"
                f"Ee movie request cheyyan thazhe ulla button click cheyyuka, "
                f"njangal ready aakumbol automatic ayi DM ayakkam.\n\n{config.FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📩 Request this movie", callback_data=f"reqconfirm_{req_id}")
                ]]),
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Ee link expire aayi / file kandilla.\n\n"
                f"Ee movie-yude peru type cheythu bot-inu ayakkuka, njangal athu veendum ready aakki "
                f"ningalkku kittum vidham DM ayakkam.\n\n{config.FOOTER}"
            )
        return

    if len(files) == 1:
        await deliver_single_file(chat_id, movie["title"], files[0], context, movie.get("poster_file_id"))
        return

    buttons = [
        [InlineKeyboardButton(quality_button_text(f), callback_data=f"pick_{f['id']}")]
        for f in files
    ]
    caption = (
        f"📂 <b>Movie Selection:</b> {movie['title']}\n\n"
        f"Multiple quality options are available for this title. "
        f"Please select your preferred resolution below to start the download.\n\n{config.FOOTER}"
    )
    logo_bytes = get_logo_thumbnail()       # full quality, sent as the document itself
    preview_bytes = get_menu_thumbnail()     # small square preview shown next to the text
    if logo_bytes:
        await context.bot.send_document(
            chat_id, document=logo_bytes, filename="poster.jpg",
            thumbnail=preview_bytes or logo_bytes,
            caption=caption, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await context.bot.send_message(
            chat_id, caption,
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
        )


async def log_search(context: ContextTypes.DEFAULT_TYPE, user, query_text: str, found: bool):
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
        await context.bot.send_message(config.LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning(f"Failed to log to log channel: {e}")


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(config.LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning(f"Failed to log event: {e}")


# ---------------- Private channel: capture admin uploads ----------------
# Admin workflow: send the poster PHOTO + the movie VIDEO/DOCUMENT together as one
# album (select both in Telegram and send at once) to the PRIVATE channel.
# Put the movie name in the photo's caption. The bot links them automatically.
#
# - If the title matches an EXISTING movie (partial/similar match), the new
#   file is just added as another size/quality under that same movie — NO
#   new post goes to the main channel. Include a quality tag anywhere in the
#   caption (480p/720p/1080p/2K/4K/HDRip/WEB-DL/BluRay/CAM) and it will be
#   shown as the option label; otherwise the file size is shown instead.
# - If it's a brand new title, a new movie is created and posted once.
# - To update a movie's poster later: send a PHOTO with caption
#   "/thumb:movie name" to the private channel. The bot finds the matching
#   movie, deletes its old main-channel post, and posts a fresh one with the
#   new poster (same Get Movie button/code).

async def process_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(2)
    messages = media_groups.pop(media_group_id, [])
    if messages:
        await handle_collected_messages(messages, context)


URL_PATTERN = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+|@\w+)", re.IGNORECASE)
EMOJI_PREFIX_PATTERN = re.compile(r"^[\W_]+", re.UNICODE)
THUMB_CMD_PATTERN = re.compile(r"^/thumb:\s*(.+)$", re.IGNORECASE)
QUALITY_PATTERN = re.compile(
    r"\b(4K|2160p|1440p|1080p|720p|480p|360p|HDRip|HDCAM|WEB-?DL|WEBRip|BluRay|CAM)\b",
    re.IGNORECASE,
)


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


def extract_quality_label(text: str):
    if not text:
        return None
    m = QUALITY_PATTERN.search(text)
    return m.group(0) if m else None


# Common junk tokens found in captions/filenames (codec, source, language,
# release group, year) that should NOT become part of the movie's title —
# stripping these consistently, whether the text came from a caption or a
# raw filename, is what makes "same movie, different size" matching reliable.
FILENAME_JUNK_PATTERN = re.compile(
    r"\b(x264|x265|HEVC|AAC|ESub|ESubs|Dual Audio|DDP?5\.1|AMZN|NF|HD ?Print|Print|ORG|"
    r"Hindi|Tamil|Telugu|Malayalam|Kannada|English|"
    r"HDRip|HDCAM|WEB-?DL|WEBRip|BluRay|BRRip|DVDRip|CAM|PreDVD|"
    r"4K|2160p|1440p|1080p|720p|480p|360p|"
    r"19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)


def build_title_and_quality(raw_caption: str, file_name: str):
    """Works from the caption if there is one, otherwise falls back to the
    file's own filename — either way the result goes through the same
    cleanup, so 'Spiderman 720p' (caption) and 'Spiderman.1080p.WEB-DL.mkv'
    (filename) both normalize down to just 'Spiderman'."""
    quality_label = extract_quality_label(raw_caption or "") or extract_quality_label(file_name or "")

    source = raw_caption if raw_caption else (file_name or "")
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), "") if source else ""
    if not first_line:
        return "Untitled", quality_label

    cleaned = URL_PATTERN.sub("", first_line)
    cleaned = EMOJI_PREFIX_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\.\w{2,4}$", "", cleaned)      # drop file extension, if any
    cleaned = re.sub(r"[._]+", " ", cleaned)           # dots/underscores -> spaces
    cleaned = FILENAME_JUNK_PATTERN.sub("", cleaned)   # strip quality/codec/lang/year junk
    cleaned = re.sub(r"[\[\](){}]", "", cleaned)
    cleaned = re.sub(r"[-–—]{1,}", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|•")
    return (cleaned or "Untitled"), quality_label


NEW_ARRIVAL_TEXT = "🆕 Latest movie collection vannittundu!"


def movie_caption(title: str) -> str:
    return f"🎬 <b>{title}</b>\n\n{NEW_ARRIVAL_TEXT}\n{config.FOOTER}"


CODE_LEN = 8  # matches db.generate_code()


def _b64_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _b64_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def encode_start_payload(code: str, title: str, max_len: int = 64) -> str:
    """CODE + base64(title), so even if the database entry is ever lost, the
    bot can still recover the movie's title straight from the button/link
    itself — no DB lookup, no asking the user to retype anything. Telegram
    limits start payloads (and callback_data) to 64 bytes total, so the
    title is truncated as needed to fit within max_len."""
    remaining = max_len - CODE_LEN
    title_part = title
    while remaining > 0:
        encoded = _b64_encode(title_part)
        if len(encoded) <= remaining:
            return code + encoded
        title_part = title_part[:-1]
    return code  # extremely unlikely fallback: no room for any title chars


def decode_start_payload(payload: str):
    """Returns (code, title_or_None)."""
    code = payload[:CODE_LEN]
    title = None
    rest = payload[CODE_LEN:]
    if rest:
        try:
            title = _b64_decode(rest)
        except Exception:
            title = None
    return code, title


def get_movie_button(code: str, title: str) -> InlineKeyboardMarkup:
    payload = encode_start_payload(code, title)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Get Movie", url=f"https://t.me/{config.BOT_USERNAME}?start={payload}")
    ]])


async def notify_requesters(title: str, code: str, context: ContextTypes.DEFAULT_TYPE):
    matches = db.get_matching_requests(title)
    for req in matches:
        try:
            await context.bot.send_message(
                req["user_id"],
                f"🎉 Ningal request cheytha movie ready aayi!\n\n"
                f"🎬 <b>{title}</b>\n\n{config.FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_movie_button(code, title),
            )
        except TelegramError as e:
            logger.info(f"Could not DM requester {req['user_id']}: {e}")
        db.mark_request_fulfilled(req["id"])


async def handle_thumb_command(messages: list, query_title: str, context: ContextTypes.DEFAULT_TYPE):
    new_poster_id = None
    for msg in messages:
        if msg.photo:
            new_poster_id = msg.photo[-1].file_id
            break

    if not new_poster_id:
        await context.bot.send_message(
            config.PRIVATE_CHANNEL_ID,
            "⚠️ /thumb command-inu oru photo koodi venam (photo-de caption-il /thumb:MovieName ittu send cheyyuka).",
        )
        return

    movie = db.find_movie_by_title(query_title)
    if not movie:
        await context.bot.send_message(
            config.PRIVATE_CHANNEL_ID, f"⚠️ '{query_title}' ennu match aavunna movie kandilla."
        )
        return

    if movie.get("main_message_id"):
        try:
            await context.bot.delete_message(config.MAIN_CHANNEL_ID, movie["main_message_id"])
        except TelegramError as e:
            logger.info(f"Could not delete old main channel message: {e}")

    try:
        sent = await context.bot.send_photo(
            config.MAIN_CHANNEL_ID, new_poster_id,
            caption=movie_caption(movie["title"]),
            parse_mode=ParseMode.HTML,
            reply_markup=get_movie_button(movie["code"], movie["title"]),
        )
        db.update_movie_poster(movie["code"], new_poster_id, sent.message_id)
        await log_event(context, f"🖼 <b>Thumbnail updated</b>\n\n🎬 {movie['title']}")
    except TelegramError as e:
        logger.error(f"Failed to post updated poster: {e}")


async def handle_collected_messages(messages: list, context: ContextTypes.DEFAULT_TYPE):
    poster_file_id = None
    thumb_file_id = None
    file_id = None
    file_type = None
    file_size = None
    file_name = None
    raw_caption = None
    thumb_cmd_title = None

    for msg in messages:
        if msg.caption:
            raw_caption = msg.caption.strip()
            m = THUMB_CMD_PATTERN.match(raw_caption)
            if m:
                thumb_cmd_title = m.group(1).strip()
        if msg.photo:
            poster_file_id = msg.photo[-1].file_id
        elif msg.video:
            file_id = msg.video.file_id
            file_type = "video"
            file_size = msg.video.file_size
            file_name = getattr(msg.video, "file_name", None)
            if msg.video.thumbnail:
                thumb_file_id = msg.video.thumbnail.file_id
        elif msg.document:
            file_id = msg.document.file_id
            file_type = "document"
            file_size = msg.document.file_size
            file_name = msg.document.file_name
            if msg.document.thumbnail:
                thumb_file_id = msg.document.thumbnail.file_id

    # ---- /thumb:movie name -> update poster of an existing movie ----
    if thumb_cmd_title:
        await handle_thumb_command(messages, thumb_cmd_title, context)
        return

    if not file_id:
        return  # only a poster arrived, nothing to link yet

    # Same cleanup logic runs whether the title came from the caption or,
    # if there was no caption at all, from the file's own filename — this
    # keeps title matching reliable across different-size uploads.
    title, quality_label = build_title_and_quality(raw_caption, file_name)

    existing = db.find_movie_by_title(title)

    if existing:
        # Same movie already posted — just attach this as another quality
        # option, do NOT create a new main channel post.
        db.add_movie_file(existing["code"], file_id, file_type, file_size, quality_label)
        await log_event(
            context,
            f"➕ <b>Extra file added</b>\n\n🎬 {existing['title']}\n"
            f"⚡ Quality: {quality_label or 'not tagged'}\n"
            f"📦 Size: {format_size(file_size) or 'unknown'}",
        )
        return

    # Brand new movie
    code = db.create_movie(title, poster_file_id)
    db.add_movie_file(code, file_id, file_type, file_size, quality_label)
    await log_event(
        context,
        f"🆕 <b>New movie created</b>\n\n🎬 {title}\n"
        f"🔑 Code: <code>{code}</code>\n"
        f"⚡ Quality: {quality_label or 'not tagged'}\n"
        f"📦 Size: {format_size(file_size) or 'unknown'}\n\n"
        f"<i>If you upload another size for this SAME movie and it doesn't "
        f"say 'Extra file added' in the next log, the titles didn't match — "
        f"compare the title shown here with the next one.</i>",
    )

    caption = movie_caption(title)
    button = get_movie_button(code, title)

    try:
        if poster_file_id:
            sent = await context.bot.send_photo(
                config.MAIN_CHANNEL_ID, poster_file_id, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
        elif thumb_file_id:
            tg_file = await context.bot.get_file(thumb_file_id)
            photo_bytes = await tg_file.download_as_bytearray()
            sent = await context.bot.send_photo(
                config.MAIN_CHANNEL_ID, bytes(photo_bytes), caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
        else:
            sent = await context.bot.send_message(
                config.MAIN_CHANNEL_ID, caption,
                parse_mode=ParseMode.HTML, reply_markup=button,
            )
        db.update_movie_message_id(code, sent.message_id)
    except TelegramError as e:
        logger.error(f"Failed to post to main channel: {e}")

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

    code, fallback_title = decode_start_payload(context.args[0])

    if not await is_joined(context.bot, user.id):
        await update.message.reply_text(
            f"⚠️ Ee movie edukkan main channel join cheyyanam.\n\n{config.FOOTER}",
            reply_markup=join_keyboard(code, fallback_title),
        )
        return

    await show_quality_menu(update.effective_chat.id, code, context, user.id, fallback_title)


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    payload = query.data.split("check_", 1)[1]
    code, fallback_title = decode_start_payload(payload)
    user_id = query.from_user.id

    if await is_joined(context.bot, user_id):
        await query.answer("✅ Confirm cheythu!")
        await query.message.delete()
        await show_quality_menu(query.message.chat.id, code, context, user_id, fallback_title)
    else:
        await query.answer("❌ Ninnal ippozhum channel join cheythittilla!", show_alert=True)


# ---------------- Search by movie name (DM only) ----------------

async def search_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        return

    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)

    results = db.search_movies(query_text)
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
            await update.message.reply_text(
                f"⚠️ Ee movie edukkan main channel join cheyyanam.\n\n{config.FOOTER}",
                reply_markup=join_keyboard(results[0]["code"], results[0]["title"]),
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
        await show_quality_menu(update.effective_chat.id, results[0]["code"], context, user.id, results[0]["title"])
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
    movie = db.get_movie(code)
    fallback_title = movie["title"] if movie else None
    await show_quality_menu(query.message.chat.id, code, context, user_id, fallback_title)


async def pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a specific quality/size option from the selection menu."""
    query = update.callback_query
    file_row_id = int(query.data.split("pick_", 1)[1])
    user_id = query.from_user.id

    if not await is_joined(context.bot, user_id):
        await query.answer("❌ Ninnal ippozhum channel join cheythittilla!", show_alert=True)
        return

    record = db.get_movie_file(file_row_id)
    if not record:
        await query.answer("⚠️ File kandilla / expire aayi.", show_alert=True)
        return

    movie = db.get_movie(record["code"])
    await query.answer()
    await deliver_single_file(
        query.message.chat.id, movie["title"] if movie else "Movie", record, context,
        movie.get("poster_file_id") if movie else None,
    )


async def request_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "/broadcast - (reply to a message) send it to all users\n"
        "/dm <user_id> <text> - message ONE specific user (or reply + /dm <user_id> for media)\n\n"
        "Private channel commands:\n"
        "• Poster + video/doc with movie name caption → new movie post\n"
        "• Add quality tag (720p/1080p/4K etc.) in caption → shown as an option label\n"
        "• Another file with similar movie name → auto-added as extra quality, no duplicate post\n"
        "• Photo with caption /thumb:movie name → replaces that movie's poster\n\n"
        f"{config.FOOTER}"
    )
    await update.message.reply_text(text)


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats()
    await update.message.reply_text(
        f"👥 Total Users: {stats['users']}\n"
        f"🎬 Total Movies: {stats['movies']}\n"
        f"📦 Total Files: {stats['files']}\n\n"
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
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Broadcast complete.\nSent: {sent}\nFailed/removed: {failed}\n\n{config.FOOTER}"
    )


@admin_only
async def dm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a direct message to ONE specific user (not everyone like /broadcast).
    Usage: /dm <user_id> <message>
    Or: reply to a photo/video/file with /dm <user_id> to forward that instead."""

    if update.message.reply_to_message:
        if not context.args:
            await update.message.reply_text(
                "Usage: oru message/file-inu reply cheythu:\n/dm <user_id>"
            )
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ Correct ayittulla user ID kodukkuka (numbers mathram).")
            return

        try:
            await update.message.reply_to_message.copy(chat_id=target_id)
            await update.message.reply_text(f"✅ Send cheythu → user {target_id}")
        except TelegramError as e:
            await update.message.reply_text(f"❌ Send cheyyan pattiyilla: {e}")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/dm <user_id> <message text>\n\n"
            "Allenkil, oru photo/video/file-inu reply cheythu:\n"
            "/dm <user_id>\n\n"
            "User ID kittan: /requests command allenkil Log Channel-il ninnu."
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Correct ayittulla user ID kodukkuka (numbers mathram).")
        return

    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(target_id, text)
        await update.message.reply_text(f"✅ Send cheythu → user {target_id}")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Send cheyyan pattiyilla: {e}")


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
    app.add_handler(CommandHandler("dm", dm_command))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^check_"))
    app.add_handler(CallbackQueryHandler(getfile_callback, pattern=r"^getfile_"))
    app.add_handler(CallbackQueryHandler(pick_callback, pattern=r"^pick_"))
    app.add_handler(CallbackQueryHandler(request_confirm_callback, pattern=r"^reqconfirm_"))
    app.add_handler(MessageHandler(filters.Chat(chat_id=config.PRIVATE_CHANNEL_ID), private_channel_handler))
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
