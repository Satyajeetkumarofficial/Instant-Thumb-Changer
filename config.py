# config.py

# REQUIRED
BOT_TOKEN = "8090736841:AAEi5FkCzBhccIU8RbZBxmPTDq2V7a2c4UE"   # BotFather ka token daalein
PUBLIC_URL = "https://small-kiley-santoshh-f856e5f5.koyeb.app"  # Koyeb service ka HTTPS URL

# Optional (safe defaults; env vars override)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-this-secret")  # set a strong random value
PORT = int(os.getenv("PORT", "8080"))

# Features
THUMB_MAX_MB = float(os.getenv("THUMB_MAX_MB", "5"))  # Poster (separate message) max size
DEFAULT_STYLE = os.getenv("DEFAULT_STYLE", "yt")       # yt | yt_fit | square | auto
POSTER_MODE_DEFAULT = os.getenv("POSTER_MODE_DEFAULT", "false").lower() in ("1", "true", "yes", "on")

# Telegram hard limits (do not change)
THUMB_TELEGRAM_MAX_BYTES = 200 * 1024     # attached thumbnail max size
THUMB_TELEGRAM_MAX_DIM = (320, 320)       # max side
TARGET_YT_DIM = (320, 180)                # 16:9 YouTube-like

# Derived
WEBHOOK_PATH = f"webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = PUBLIC_URL.rstrip("/") + "/" + WEBHOOK_PATH

ALLOWED_UPDATES = ["message"]
