import os

# ---- Required environment variables (set in Render dashboard) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"]          # bot username without @, e.g. MyMovieBot
WEBHOOK_URL = os.environ["WEBHOOK_URL"]            # e.g. https://your-app.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# Private channel where ADMIN uploads poster + movie file (bot must be admin here)
PRIVATE_CHANNEL_ID = int(os.environ["PRIVATE_CHANNEL_ID"])   # e.g. -1001234567890

# Main public channel where the bot auto-posts the poster (bot must be admin here)
MAIN_CHANNEL_ID = int(os.environ["MAIN_CHANNEL_ID"])
MAIN_CHANNEL_LINK = os.environ["MAIN_CHANNEL_LINK"]          # https://t.me/yourchannel

# Channel where user search activity gets logged (bot must be admin here)
LOG_CHANNEL_ID = int(os.environ["LOG_CHANNEL_ID"])           # e.g. -1004362336237

# Channel where confirmed movie requests get posted (bot must be admin here)
REQUEST_CHANNEL_ID = int(os.environ["REQUEST_CHANNEL_ID"])   # e.g. -1004475375817

# Comma separated extra force-subscribe channel ids (optional). Main channel is always included.
_extra = os.environ.get("FORCE_SUB_CHANNEL_IDS", "")
FORCE_SUB_CHANNEL_IDS = list({MAIN_CHANNEL_ID, *[int(x) for x in _extra.split(",") if x.strip()]})

# Comma separated admin telegram user ids, e.g. "123456789,987654321"
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", 120))
FOOTER = "Powered by Levino"
