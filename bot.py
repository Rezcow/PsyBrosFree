import os
import re
import uuid
import logging
import httpx

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent
)
from telegram.ext import (
    Application, MessageHandler, ContextTypes, filters, InlineQueryHandler
)

# -------- Logging --------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("odesli-bot-plus")

BOT_TOKEN = os.environ["BOT_TOKEN"]

# -------- Utils --------
URL_RE = re.compile(r"https?://\S+")

def find_urls(text: str) -> list[str]:
    if not text:
        return []
    urls = [u.rstrip(").,>]}\"'") for u in URL_RE.findall(text)]
    # opcional: dedup preservando orden
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def nice_name(key: str) -> str:
    k = key.lower()
    # renombres pedidos
    if k == "spotify": return "Espotifai"
    if k == "youtube": return "Yutú"
    if k == "youtubemusic": return "Yutúmusic"
    if k == "applemusic": return "Manzanita"
    # nombres bonitos por defecto
    mapping = {
        "amazonmusic": "Amazon Music", "amazonstore": "Amazon Store",
        "anghami": "Anghami", "bandcamp": "Bandcamp", "deezer": "Deezer",
        "napster": "Napster", "pandora": "Pandora", "soundcloud": "SoundCloud",
        "tidal": "Tidal", "itunes": "iTunes", "yandex": "Yandex",
        "boomplay": "Boomplay", "audius": "Audius",
    }
    if k in mapping: return mapping[k]
    # fallback: capitalizar
    return key.capitalize()

FAVORITES_ORDER = ["spotify", "youtubemusic", "applemusic", "youtube"]

async def fetch_odesli(url: str):
    """Devuelve (keyboard, title, artist, cover_url) o (None, None, None, None)."""
    api = f"https://api.song.link/v1-alpha.1/links?url={url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api, timeout=12)
        if r.status_code != 200:
            return None, None, None, None
        data = r.json()

        links = data.get("linksByPlatform", {}) or {}
        if not links:
            return None, None, None, None

        # ordenar: favoritos primero, resto alfabético
        keys = list(links.keys())
        keys_sorted = (
            [k for k in FAVORITES_ORDER if k in keys] +
            sorted([k for k in keys if k not in FAVORITES_ORDER], key=lambda x: nice_name(x))
        )

        botones, fila = [], []
        for k in keys_sorted:
            u = links[k].get("url")
            if not u:
                continue
            label = nice_name(k)
            fila.append(InlineKeyboardButton(text=label, url=u))
            if len(fila) == 3:
                botones.append(fila); fila = []
        if fila:
            botones.append(fila)
        keyboard = InlineKeyboardMarkup(botones) if botones else None

        # metadatos para título/portada
        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        title = entity.get("title")
        artist = entity.get("artistName")
        cover = entity.get("thumbnailUrl")

        return keyboard, title, artist, cover
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None, None, None, None

# -------- Chat handler (multi-URL) --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = find_urls(update.message.text if update.message else "")
    if not urls:
        return
    for url in urls:
        keyboard, title, artist, cover = await fetch_odesli(url)
        if not keyboard:
            await update.message.reply_text("😕 busqué y busqué pero no encontré.")
            continue

        caption_title = None
        if title and artist:
            caption_title = f"🎵 {title} — {artist}\n🎶 Disponible en:"
        elif title:
            caption_title = f"🎵 {title}\n🎶 Disponible en:"
        else:
            caption_title = "🎶 Disponible en:"

        # si hay portada, la usamos como mensaje principal con el teclado
        if cover:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=cover,
                    caption=caption_title,
                    reply_markup=keyboard
                )
                continue
            except Exception as e:
                log.info(f"No pude usar la portada, envío texto. {e}")

        await update.message.reply_text(caption_title, reply_markup=keyboard)

# -------- Inline mode --------
async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.inline_query.query or "").strip()
    urls = find_urls(q)
    if not urls:
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    url = urls[0]  # tomamos el primero
    keyboard, title, artist, cover = await fetch_odesli(url)
    if not keyboard:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    caption = "🎶 Disponible en:"
    if title and artist:
        caption = f"🎵 {title} — {artist}\n🎶 Disponible en:"
    elif title:
        caption = f"🎵 {title}\n🎶 Disponible en:"

    results = []
    rid = str(uuid.uuid4())

    if cover:
        results.append(
            InlineQueryResultPhoto(
                id=rid,
                photo_url=cover,
                thumb_url=cover,
                caption=caption,
                reply_markup=keyboard,
                title=title or "Plataformas"
            )
        )
    else:
        results.append(
            InlineQueryResultArticle(
                id=rid,
                title=title or "Plataformas",
                input_message_content=InputTextMessageContent(caption),
                reply_markup=keyboard,
                description="Enviar accesos a otras plataformas"
            )
        )

    await update.inline_query.answer(results, cache_time=10, is_personal=True)

# -------- Post-init / main --------
async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook eliminado; inline + polling listos.")
    except Exception as e:
        log.warning(f"No pude limpiar webhook: {e}")

if __name__ == "__main__":
    app = (Application.builder().token(BOT_TOKEN).post_init(_post_init).build())
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    log.info("✅ Bot listo. Escribe o usa @Bot en cualquier chat.")
    app.run_polling(drop_pending_updates=True)
