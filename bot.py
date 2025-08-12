import os
import re
import uuid
import logging
import httpx
from collections import deque
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent
)
from telegram.ext import (
    Application, MessageHandler, ContextTypes, filters,
    InlineQueryHandler, CallbackQueryHandler
)

# -------- Config / Logging --------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("odesli-bot-plus")

BOT_TOKEN = os.environ["BOT_TOKEN"]
COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL").upper()  # << región preferida (CL por defecto)

# -------- Utils --------
URL_RE = re.compile(r"https?://\S+")

# dominios musicales (para ignorar Instagram, etc.)
MUSIC_DOMAINS = (
    "spotify.com",
    "music.apple.com", "itunes.apple.com", "geo.music.apple.com",
    "youtube.com", "youtu.be", "music.youtube.com",
    "soundcloud.com", "bandcamp.com", "tidal.com", "deezer.com",
    "pandora.com", "yandex", "napster.com", "audiomack.com",
    "anghami.com", "boomplay.com", "amazonmusic.com", "music.amazon.",
    "audius.co",
)

def is_music_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in MUSIC_DOMAINS)
    except Exception:
        return False

def find_urls(text: str) -> list[str]:
    if not text:
        return []
    urls = [u.rstrip(").,>]}\"'") for u in URL_RE.findall(text)]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def nice_name(key: str) -> str:
    k = key.lower()
    if k == "spotify": return "Espotifai"
    if k == "youtube": return "Yutú"
    if k == "youtubemusic": return "Yutúmusic"
    if k == "applemusic": return "Manzanita"
    if k == "soundcloud": return "SounClou"
    mapping = {
        "amazonmusic": "Amazon Music", "amazonstore": "Amazon Store",
        "anghami": "Anghami", "bandcamp": "Bandcamp", "deezer": "Deezer",
        "napster": "Napster", "pandora": "Pandora",
        "tidal": "Tidal", "itunes": "iTunes", "yandex": "Yandex",
        "boomplay": "Boomplay", "audius": "Audius", "audiomack": "Audiomack",
    }
    return mapping.get(k, key.capitalize())

FAVS_LOWER = ["spotify", "youtube", "youtubemusic", "applemusic", "soundcloud"]

def sort_keys(links: dict) -> list[str]:
    keys = list(links.keys())
    lower_to_orig = {k.lower(): k for k in keys}
    fav_present = [lower_to_orig[k] for k in FAVS_LOWER if k in lower_to_orig]
    others = [k for k in keys if k.lower() not in set(FAVS_LOWER)]
    others_sorted = sorted(others, key=lambda x: nice_name(x.lower()))
    return fav_present + others_sorted

# ---- Regionalización Apple Music ----
def _regionalize_apple(url: str) -> str:
    """Forza región en enlaces de Apple Music a COUNTRY y limpia tracking."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if "music.apple.com" in host or "geo.music.apple.com" in host or "itunes.apple.com" in host:
            # host: usar music.apple.com
            host = "music.apple.com"
            # path: /{cc}/resto ... -> cambia cc a COUNTRY (si existe)
            parts = p.path.strip("/").split("/", 1)
            if parts and len(parts[0]) == 2:
                parts[0] = COUNTRY.lower()
                new_path = "/" + "/".join(parts)
            else:
                new_path = f"/{COUNTRY.lower()}/{p.path.strip('/')}"
            # query: conservar solo ?i=... (si existe)
            q = parse_qs(p.query)
            new_q = {}
            if "i" in q and q["i"]:
                new_q["i"] = q["i"][0]
            return urlunparse((p.scheme, host, new_path, "", urlencode(new_q) if new_q else "", ""))
    except Exception as e:
        log.debug(f"No pude regionalizar Apple Music: {e}")
    return url

def regionalize_links(links: dict) -> dict:
    """Crea una copia con Apple Music ajustado a la región preferida."""
    out = {}
    for k, info in links.items():
        url = info.get("url")
        if not url:
            continue
        if k.lower() in ("applemusic", "itunes"):
            url = _regionalize_apple(url)
        out[k] = {**info, "url": url}
    return out

# almacenamiento simple para callbacks
STORE: dict[str, dict] = {}
ORDER = deque(maxlen=300)
def remember_links(links: dict) -> str:
    key = uuid.uuid4().hex
    STORE[key] = links
    ORDER.append(key)
    while len(STORE) > ORDER.maxlen:
        old = ORDER.popleft()
        STORE.pop(old, None)
    return key

def build_keyboard(links: dict, show_all: bool, key: str) -> InlineKeyboardMarkup:
    sorted_keys = sort_keys(links)
    keys_to_show = (
        sorted_keys if show_all else [k for k in sorted_keys if k.lower() in set(FAVS_LOWER)]
    )

    botones, fila = [], []
    for k in keys_to_show:
        url = links[k].get("url")
        if not url:
            continue
        label = nice_name(k)
        fila.append(InlineKeyboardButton(text=label, url=url))
        if len(fila) == 3:
            botones.append(fila); fila = []
    if fila:
        botones.append(fila)

    if not show_all and len(keys_to_show) < len(sorted_keys):
        botones.append([InlineKeyboardButton("Más opciones ▾", callback_data=f"more|{key}")])
    elif show_all:
        botones.append([InlineKeyboardButton("◀ Menos opciones", callback_data=f"less|{key}")])

    return InlineKeyboardMarkup(botones)

async def fetch_odesli(url: str):
    """Devuelve (links_by_platform, title, artist, cover_url)."""
    api = "https://api.song.link/v1-alpha.1/links"
    params = {"url": url, "userCountry": COUNTRY}   # << fuerza región
    headers = {"Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api, params=params, headers=headers, timeout=12)
        if r.status_code != 200:
            return None, None, None, None
        data = r.json()
        raw_links = data.get("linksByPlatform", {}) or {}
        links = regionalize_links(raw_links)  # ajusta Apple Music
        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        return links or None, entity.get("title"), entity.get("artistName"), entity.get("thumbnailUrl")
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None, None, None, None

# -------- Chat handler (multi-URL con filtro musical) --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = find_urls(update.message.text if update.message else "")
    if not urls:
        return
    for url in urls:
        if not is_music_url(url):
            continue  # silencio para no-musicales (Instagram, etc.)

        links, title, artist, cover = await fetch_odesli(url)
        if not links:
            await update.message.reply_text("😕 busqué y busqué pero no encontré.")
            continue

        key = remember_links(links)
        keyboard = build_keyboard(links, show_all=False, key=key)

        caption = "🎶 Disponible en:"
        if title and artist:
            caption = f"🎵 {title} — {artist}\n🎶 Disponible en:"
        elif title:
            caption = f"🎵 {title}\n🎶 Disponible en:"

        if cover:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=cover,
                    caption=caption,
                    reply_markup=keyboard
                )
                continue
            except Exception as e:
                log.info(f"No pude usar la portada, envío texto. {e}")

        await update.message.reply_text(caption, reply_markup=keyboard)

# -------- Inline mode --------
async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.inline_query.query or "").strip()
    urls = find_urls(q)
    if not urls:
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    url = urls[0]
    if not is_music_url(url):
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    links, title, artist, cover = await fetch_odesli(url)
    if not links:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    key = remember_links(links)
    keyboard = build_keyboard(links, show_all=False, key=key)

    caption = "🎶 Disponible en:"
    if title and artist:
        caption = f"🎵 {title} — {artist}\n🎶 Disponible en:"
    elif title:
        caption = f"🎵 {title}\n🎶 Disponible en:"

    rid = str(uuid.uuid4())
    if cover:
        results = [InlineQueryResultPhoto(
            id=rid, photo_url=cover, thumb_url=cover,
            caption=caption, reply_markup=keyboard, title=title or "Plataformas"
        )]
    else:
        results = [InlineQueryResultArticle(
            id=rid, title=title or "Plataformas",
            input_message_content=InputTextMessageContent(caption),
            reply_markup=keyboard, description="Enviar accesos a otras plataformas"
        )]
    await update.inline_query.answer(results, cache_time=10, is_personal=True)

# -------- Callbacks (expandir/colapsar) --------
async def toggle_more_less(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    data = cq.data or ""
    if not (data.startswith("more|") or data.startswith("less|")):
        return
    _, key = data.split("|", 1)
    links = STORE.get(key)
    if not links:
        return

    show_all = data.startswith("more|")
    keyboard = build_keyboard(links, show_all=show_all, key=key)

    try:
        if cq.inline_message_id:
            await context.bot.edit_message_reply_markup(
                inline_message_id=cq.inline_message_id,
                reply_markup=keyboard
            )
        else:
            await context.bot.edit_message_reply_markup(
                chat_id=cq.message.chat_id,
                message_id=cq.message.message_id,
                reply_markup=keyboard
            )
    except Exception as e:
        log.warning(f"No pude editar el teclado: {e}")

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
    app.add_handler(CallbackQueryHandler(toggle_more_less))
    log.info("✅ Bot listo. Escribe o usa @Bot en cualquier chat.")
    app.run_polling(drop_pending_updates=True)
