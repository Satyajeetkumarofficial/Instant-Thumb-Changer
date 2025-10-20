import logging
from io import BytesIO
from typing import Tuple

import config

from PIL import Image, ImageFilter, ImageOps
from pillow_heif import register_heif_opener

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# Logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("thumb-bot")

# HEIC/HEIF support
register_heif_opener()

# ---------------- Telegram limits ----------------
THUMB_TELEGRAM_MAX_BYTES = 200 * 1024
THUMB_TELEGRAM_MAX_DIM: Tuple[int, int] = (320, 320)
TARGET_YT_DIM: Tuple[int, int] = (320, 180)
THUMB_ACCEPT_MAX_BYTES = int(config.THUMB_MAX_MB * 1024 * 1024)

# ---------------- In-memory stores (ephemeral) ----------------
USER_THUMB_COMPRESSED: dict[int, bytes] = {}  # <=200KB JPEG for Telegram
USER_THUMB_ORIGINAL: dict[int, bytes] = {}    # poster image (<=5MB)
USER_SETTINGS: dict[int, dict] = {}           # {"poster_mode": bool, "thumb_style": str}
AWAITING_THUMB: set[int] = set()

# ---------------- Helpers ----------------
def get_settings(uid: int) -> dict:
    s = USER_SETTINGS.get(uid)
    if not s:
        s = {
            "poster_mode": bool(config.POSTER_MODE_DEFAULT),
            "thumb_style": str(config.DEFAULT_THUMB_STYLE or "yt").lower(),
        }
        USER_SETTINGS[uid] = s
    return s

def load_image_any(image_bytes: bytes) -> Image.Image:
    im = Image.open(BytesIO(image_bytes))
    im = ImageOps.exif_transpose(im)
    if getattr(im, "is_animated", False):
        im.seek(0)
    return im.convert("RGB")

def jpeg_fit_under(bytes_target: int, img: Image.Image) -> bytes:
    out = BytesIO()
    for q in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20):
        out.seek(0); out.truncate(0)
        img.save(out, format="JPEG", quality=q, optimize=True, progressive=True, subsampling=2)
        if out.tell() <= bytes_target:
            return out.getvalue()
    return out.getvalue()

def make_thumb_auto(img: Image.Image) -> Image.Image:
    im = img.copy()
    im.thumbnail(THUMB_TELEGRAM_MAX_DIM, Image.LANCZOS)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im

def make_thumb_square(img: Image.Image) -> Image.Image:
    im = ImageOps.fit(img, (320, 320), method=Image.LANCZOS, centering=(0.5, 0.5))
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im

def make_thumb_yt_cover(img: Image.Image) -> Image.Image:
    im = ImageOps.fit(img, TARGET_YT_DIM, method=Image.LANCZOS, centering=(0.5, 0.5))
    im = im.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    return im

def make_thumb_yt_fit(img: Image.Image) -> Image.Image:
    W, H = TARGET_YT_DIM
    bg = ImageOps.fit(img, (W, H), method=Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=12))
    fg = img.copy()
    fg.thumbnail((W, H), Image.LANCZOS)
    x = (W - fg.width) // 2
    y = (H - fg.height) // 2
    bg.paste(fg, (x, y))
    bg = bg.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=3))
    return bg

def prepare_thumbnail(image_bytes: bytes, style: str) -> bytes:
    base = load_image_any(image_bytes)
    s = (style or "yt").lower()
    if s == "yt":
        im = make_thumb_yt_cover(base)
    elif s in ("yt_fit", "ytfit", "yt-fit"):
        im = make_thumb_yt_fit(base)
    elif s == "square":
        im = make_thumb_square(base)
    else:
        im = make_thumb_auto(base)
    return jpeg_fit_under(THUMB_TELEGRAM_MAX_BYTES, im)

def build_tg_file_url(bot_token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

# ---------------- Handlers ----------------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! 👋\n"
        "Yeh bot aapke video/document ka thumbnail YouTube-style (16:9) ya square/auto me attach karta hai.\n"
        "Server-side copy: bade files download/upload nahi hote.\n\n"
        "Commands:\n"
        "• /setthumb – thumbnail/poster set karein (photo ya image document bhejein)\n"
        "• /showthumb – current thumbnail/poster dekhein\n"
        "• /clearthumb – thumbnail/poster hataayein\n"
        "• /poster – poster mode ON/OFF (poster = 5MB tak high-res photo, separate message)\n"
        "• /style yt | yt_fit | square | auto – thumbnail style set karein\n\n"
        "Note: Telegram attached thumbnail limit = JPEG, ≤200KB, side ≤320. Poster 5MB tak allowed (separate)."
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
    mem = BytesIO()
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
    mem = BytesIO()
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
        thumb_if = InputFile(BytesIO(thumb_bytes), filename="thumb.jpg") if thumb_bytes else None

        # 3) Optional poster
        settings = get_settings(uid)
        if settings.get("poster_mode") and uid in USER_THUMB_ORIGINAL:
            try:
                await msg.reply_photo(
                    photo=InputFile(BytesIO(USER_THUMB_ORIGINAL[uid]), filename="poster.jpg")
                )
            except Exception as e:
                log.warning("Poster send failed: %s", e)

        # 4) Re-send media with new thumbnail (Telegram internal fetch)
        if msg.video:
            await msg.reply_video(
                video=file_url,
                caption=caption,
                supports_streaming=True,
                thumbnail=thumb_if  # JPEG ≤200KB, side ≤320
            )
        else:
            await msg.reply_document(
                document=file_url,
                caption=caption,
                thumbnail=thumb_if
            )

        await info.edit_text("Done ✅ Thumbnail updated (server-side).")
    except Exception as e:
        log.exception("Processing failed")
        await info.edit_text(
            "Error: {err}\n"
            "Note: Thumbnail must be JPEG, ≤200KB, side ≤320. Poster photo can be up to 5MB."
            .format(err=e)
        )

# ---------- Webhook bootstrap (Koyeb) ----------
async def on_startup(app: Application):
    public_url = str(config.PUBLIC_URL).rstrip("/")
    webhook_url = f"{public_url}/{config.WEBHOOK_PATH}"
    await app.bot.set_webhook(
        url=webhook_url,
        secret_token=(config.WEBHOOK_SECRET or None),
        allowed_updates=config.ALLOWED_UPDATES,
    )
    log.info("Webhook set to %s", webhook_url)

def main():
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

    # Webhook server for Koyeb
    app.post_init = on_startup  # set webhook after bot starts

    log.info("Starting webhook server on 0.0.0.0:%s path=/%s", config.PORT, config.WEBHOOK_PATH)
    app.run_webhook(
        listen="0.0.0.0",
        port=config.PORT,
        url_path=config.WEBHOOK_PATH,
        webhook_url=None,  # we set it in on_startup
        secret_token=(config.WEBHOOK_SECRET or None),
        allowed_updates=config.ALLOWED_UPDATES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
