import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image, ImageFilter, ImageOps

# Optional HEIC/HEIF support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_OK = True
except Exception as e:
    print("pillow-heif not available; HEIC/HEIF disabled:", e)
    HEIF_OK = False

# Try aiohttp for webhook
try:
    from aiohttp import web
    HAVE_AIOHTTP = True
except Exception as e:
    print("aiohttp not available; webhook server disabled, will fall back to polling:", e)
    HAVE_AIOHTTP = False

import config

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("thumb-bot")

# -------- In-memory stores (ephemeral; use Redis/DB if persistence needed) --------
USER_THUMB_COMPRESSED: dict[int, bytes] = {}  # <=200KB JPEG for attached thumbnail
USER_THUMB_ORIGINAL: dict[int, bytes] = {}    # poster image (<=5MB)
USER_SETTINGS: dict[int, dict] = {}           # {"poster_mode": bool, "thumb_style": str}
AWAITING_THUMB: set[int] = set()

THUMB_ACCEPT_MAX_BYTES = int(config.THUMB_MAX_MB * 1024 * 1024)


# -------- Helpers: settings --------
def get_settings(uid: int) -> dict:
    s = USER_SETTINGS.get(uid)
    if not s:
        s = {
            "poster_mode": config.POSTER_MODE_DEFAULT,
            "thumb_style": (config.DEFAULT_STYLE or "yt").lower(),
        }
        USER_SETTINGS[uid] = s
    return s


# -------- Image helpers --------
def load_image_any(image_bytes: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(image_bytes))
    im = ImageOps.exif_transpose(im)  # respect orientation
    if getattr(im, "is_animated", False):
        im.seek(0)  # take first frame
    return im.convert("RGB")


def jpeg_fit_under(bytes_target: int, img: Image.Image) -> bytes:
    out = io.BytesIO()
    for q in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20):
        out.seek(0); out.truncate(0)
        img.save(out, format="JPEG", quality=q, optimize=True, progressive=True, subsampling=2)
        if out.tell() <= bytes_target:
            return out.getvalue()
    return out.getvalue()


def make_thumb_auto(img: Image.Image) -> Image.Image:
    im = img.copy()
    im.thumbnail(config.THUMB_TELEGRAM_MAX_DIM, Image.LANCZOS)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im


def make_thumb_square(img: Image.Image) -> Image.Image:
    im = ImageOps.fit(img, (320, 320), method=Image.LANCZOS, centering=(0.5, 0.5))
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im


def make_thumb_yt_cover(img: Image.Image) -> Image.Image:
    im = ImageOps.fit(img, config.TARGET_YT_DIM, method=Image.LANCZOS, centering=(0.5, 0.5))
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im


def make_thumb_yt_fit(img: Image.Image) -> Image.Image:
    W, H = config.TARGET_YT_DIM
    # Background: blurred cover
    bg = ImageOps.fit(img, (W, H), method=Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=12))
    # Foreground: fit inside
    fg = img.copy()
    fg.thumbnail((W, H), Image.LANCZOS)
    x = (W - fg.width) // 2
    y = (H - fg.height) // 2
    bg.paste(fg, (x, y))
    bg = bg.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=3))
    return bg


def prepare_thumbnail(image_bytes: bytes, style: str) -> bytes:
    base = load_image_any(image_bytes)
    style = (style or "yt").lower()
    if style == "yt":
        im = make_thumb_yt_cover(base)
    elif style in ("yt_fit", "ytfit", "yt-fit"):
        im = make_thumb_yt_fit(base)
    elif style == "square":
        im = make_thumb_square(base)
    else:
        im = make_thumb_auto(base)
    return jpeg_fit_under(config.THUMB_TELEGRAM_MAX_BYTES, im)


def build_tg_file_url(bot_token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"


# -------- Handlers --------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    s = get_settings(update.effective_user.id)
    await update.message.reply_text(
        "Namaste! 👋\n"
        "Yeh bot aapke video/document ka thumbnail YouTube-style bana ke attach karta hai.\n"
        "Server-side copy: bade files download/upload nahi hote.\n\n"
        "Commands:\n"
        "• /setthumb – thumbnail/poster set karein (photo ya image document)\n"
        "• /showthumb – current thumbnail/poster dekhein\n"
        "• /clearthumb – thumbnail/poster hataayein\n"
        "• /poster – poster mode ON/OFF (poster = 5MB tak high-res photo)\n"
        "• /style yt | yt_fit | square | auto – thumbnail style set karein\n\n"
        f"Current style: {s['thumb_style']} | Poster mode: {'ON' if s['poster_mode'] else 'OFF'}\n"
        "Note: Attached thumbnail limit = JPEG, ≤200KB, side ≤320."
    )


async def poster(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = get_settings(uid)
    s["poster_mode"] = not s.get("poster_mode", False)
    await update.message.reply_text(f"Poster mode: {'ON ✅' if s['poster_mode'] else 'OFF ❌'}")


async def style_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = get_settings(uid)
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        await update.message.reply_text(
            f"Current style: {s['thumb_style']}\n"
            "Available: yt (default), yt_fit, square, auto\n"
            "Usage: /style yt"
        )
        return
    val = parts[1].strip().lower()
    if val not in ("yt", "yt_fit", "ytfit", "yt-fit", "square", "auto"):
        await update.message.reply_text("Invalid. Use: /style yt | yt_fit | square | auto")
        return
    if val in ("ytfit", "yt-fit"):
        val = "yt_fit"
    s["thumb_style"] = val
    await update.message.reply_text(f"Style set: {val} ✅")


async def clearthumb(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_THUMB_ORIGINAL.pop(uid, None)
    USER_THUMB_COMPRESSED.pop(uid, None)
    await update.message.reply_text("Thumbnail/Poster clear kar diya ✅")


async def showthumb(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = get_settings(uid)
    has_any = False
    if uid in USER_THUMB_ORIGINAL:
        has_any = True
        await update.message.reply_photo(
            photo=USER_THUMB_ORIGINAL[uid],
            caption="Poster (original, up to 5MB)"
        )
    if uid in USER_THUMB_COMPRESSED:
        has_any = True
        await update.message.reply_photo(
            photo=USER_THUMB_COMPRESSED[uid],
            caption=f"Compressed thumbnail (≤200KB, ≤320px), style: {s['thumb_style']} — yahi attach hota hai."
        )
    if not has_any:
        await update.message.reply_text("Aapne abhi tak thumbnail set nahi kiya. /setthumb use karein.")


async def setthumb(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    AWAITING_THUMB.add(uid)
    s = get_settings(uid)
    await update.message.reply_text(
        "Thik hai! Ab ek image bhejiye:\n"
        "• Photo (Telegram compress karega) ya\n"
        "• Document (image) — quality preserve hoti hai (recommended)\n\n"
        f"Poster limit: ≤ {THUMB_ACCEPT_MAX_BYTES // (1024*1024)}MB.\n"
        f"Style: {s['thumb_style']} (change via /style)."
    )


def is_image_document(update: Update) -> bool:
    msg = update.message
    return bool(msg and msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"))


async def handle_new_thumb_from_photo(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in AWAITING_THUMB:
        return
    photo = update.message.photo[-1]
    f = await photo.get_file()
    if f.file_size and f.file_size > THUMB_ACCEPT_MAX_BYTES:
        await update.message.reply_text("Image bahut badi hai. Kripya ≤5MB image bhejein (as Document).")
        return
    mem = io.BytesIO()
    await f.download_to_memory(out=mem)
    original = mem.getvalue()
    USER_THUMB_ORIGINAL[uid] = original
    s = get_settings(uid)
    USER_THUMB_COMPRESSED[uid] = prepare_thumbnail(original, s["thumb_style"])
    AWAITING_THUMB.discard(uid)
    await update.message.reply_text("Thumbnail/Poster set ho gaya ✅")


async def maybe_handle_thumb_from_document(update: Update) -> bool:
    uid = update.effective_user.id
    if uid not in AWAITING_THUMB or not is_image_document(update):
        return False
    doc = update.message.document
    if doc.file_size and doc.file_size > THUMB_ACCEPT_MAX_BYTES:
        await update.message.reply_text("Image bahut badi hai. Kripya ≤5MB image bhejein.")
        AWAITING_THUMB.discard(uid)
        return True
    f = await doc.get_file()
    mem = io.BytesIO()
    await f.download_to_memory(out=mem)
    original = mem.getvalue()
    USER_THUMB_ORIGINAL[uid] = original
    s = get_settings(uid)
    USER_THUMB_COMPRESSED[uid] = prepare_thumbnail(original, s["thumb_style"])
    AWAITING_THUMB.discard(uid)
    await update.message.reply_text("Thumbnail/Poster set ho gaya (document se) ✅")
    return True


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # If user is setting a thumbnail via image document
    if await maybe_handle_thumb_from_document(update):
        return

    msg = update.message
    if not msg:
        return
    caption = msg.caption or ""

    # Only process video or document media for re-send
    if not (msg.video or msg.document):
        return

    info = await msg.reply_text("Turbo mode: server-side copy… ⚡")

    try:
        # 1) Get Telegram file URL (no local download of big media)
        file = await (msg.video.get_file() if msg.video else msg.document.get_file())
        file_url = build_tg_file_url(context.bot.token, file.file_path)

        # 2) Prepare thumbnail InputFile (compressed) if available
        thumb_bytes = USER_THUMB_COMPRESSED.get(uid)
        thumb_if = InputFile(io.BytesIO(thumb_bytes), filename="thumb.jpg") if thumb_bytes else None

        # 3) Optional poster (send high-res photo first)
        settings = get_settings(uid)
        if settings.get("poster_mode") and uid in USER_THUMB_ORIGINAL:
            try:
                await msg.reply_photo(
                    photo=InputFile(io.BytesIO(USER_THUMB_ORIGINAL[uid]), filename="poster.jpg")
                )
            except Exception as poster_err:
                log.warning("Poster send failed: %s", poster_err)

        # 4) Re-send media with new thumbnail (Telegram internal fetch)
        if msg.video:
            await msg.reply_video(
                video=file_url,
                caption=caption,
                supports_streaming=True,
                thumbnail=thumb_if  # JPEG ≤200KB
            )
        else:
            await msg.reply_document(
                document=file_url,
                caption=caption,
                thumbnail=thumb_if
            )

        await info.edit_text("Done ✅ Thumbnail updated (server-side).")
    except Exception as e:
        await info.edit_text(
            "Error: {err}\n"
            "Note: Attached thumbnail must be JPEG, ≤200KB, side ≤320. Poster photo can be up to 5MB."
            .format(err=e)
        )


# -------- Fallback health server (no aiohttp) --------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # silence


def _start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", config.PORT), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log.info("Health server (built-in) running on http://0.0.0.0:%s", config.PORT)
    except Exception as e:
        log.warning("Health server failed to start: %s", e)


def main():
    # Basic sanity checks
    if not config.BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in config.BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in config.py or as env var.")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setthumb", setthumb))
    app.add_handler(CommandHandler("showthumb", showthumb))
    app.add_handler(CommandHandler("clearthumb", clearthumb))
    app.add_handler(CommandHandler("poster", poster))
    app.add_handler(CommandHandler("style", style_cmd))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO, handle_new_thumb_from_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, media_handler))

    if HAVE_AIOHTTP:
        # Webhook mode with aiohttp app + health endpoints
        web_app = web.Application()
        web_app.router.add_get("/", lambda request: web.Response(text="OK"))
        web_app.router.add_get("/healthz", lambda request: web.Response(text="OK"))
        log.info("Starting webhook server on port %s", config.PORT)
        log.info("Webhook URL: %s", config.WEBHOOK_URL)
        app.run_webhook(
            web_app=web_app,
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.WEBHOOK_PATH,
            webhook_url=config.WEBHOOK_URL,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=config.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
    else:
        # Polling mode + tiny health server so Koyeb checks pass
        _start_health_server()
        log.warning("Running in long polling mode (install aiohttp for webhooks).")
        app.run_polling(
            allowed_updates=config.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
