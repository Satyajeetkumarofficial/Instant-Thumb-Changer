# config.py

# REQUIRED
BOT_TOKEN = "123456:ABCDEF-your-bot-token"   # BotFather ka token daalein
PUBLIC_URL = "https://your-app-xyz.koyeb.app"  # Koyeb service ka HTTPS URL

# Security
WEBHOOK_SECRET = "replace-with-strong-random"   # strong random string (e.g., openssl rand -hex 16)
WEBHOOK_PATH = f"webhook/{WEBHOOK_SECRET or 'hook'}"

# Server
PORT = 8080
ALLOWED_UPDATES = ["message"]
LOG_LEVEL = "INFO"  # DEBUG/INFO/WARNING/ERROR

# Thumbnail/Poster settings
THUMB_MAX_MB = 5  # Poster image (separate photo) max size
DEFAULT_THUMB_STYLE = "yt"   # yt | yt_fit | square | auto
POSTER_MODE_DEFAULT = False  # Default poster mode state

# Notes:
# - Telegram thumbnail hard limit: JPEG, <=200KB, side <= 320 px (cannot be bypassed).
# - Poster image is just a separate message, can be up to THUMB_MAX_MB.
