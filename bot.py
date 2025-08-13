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
COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL").upper()  # región por defecto

# -------- Utils --------
URL_RE = re.compile(r"https?://\S+")

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
    """Fuerza región Apple Music a COUNTRY y limpia tracking."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if "music.apple.com" in host or "geo.music.apple.com" in host or "itunes.apple.com" in host:
            host = "music.apple.com"
            parts = p.path.strip("/").split("/", 1)
            if parts and len(parts[0]) == 2:
                parts[0] = COUNTRY.lower()
                new_path = "/" + "/".join(parts)
            else:
                new_path = f"/{COUNTRY.lower()}/{p.path.strip('/')}"
            # quitamos parámetros (p.ej. ?i=...) para que apunte al álbum
            return urlunparse((p.scheme, host, new_path, "", "", ""))
    except Exception as e:
        log.debug(f"No pude regionalizar Apple Music: {e}")
    return url

def regionalize_links(links: dict) -> dict:
    out = {}
    for k, info in links.items():
        url = info.get("url")
        if not url:
            continue
        if k.lower() in ("applemusic", "itunes"):
            url = _regionalize_apple(url)
        out[k] = {**info, "url": url}
    return out

# ===== Derivar enlaces de ÁLBUM por plataforma =====
ALBUM_LABEL = {
    "applemusic": "💿 Ver álbum (Manzanita)",
    "spotify": "💿 Ver álbum (Espotifai)",
    "youtubemusic": "💿 Ver álbum (Yutúmusic)",
    "youtube": "💿 Ver álbum (Yutú)",
    "soundcloud": "💿 Ver álbum (SounClou)",
}

def _album_from_apple(url: str):
    album_url = _regionalize_apple(url.split("?", 1)[0])
    album_name = None
    try:
        m = re.search(r"/album/([^/]+)/", urlparse(album_url).path)
        if m:
            album_name = m.group(1).replace("-", " ").strip().title()
    except Exception:
        pass
    return album_url, album_name

async def _album_from_spotify(url: str):
    p = urlparse(url)
    if "/album/" in p.path:
        return url, None
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=10)
        m = re.search(r"open\.spotify\.com/album/([A-Za-z0-9]+)", r.text)
        if m:
            return f"https://open.spotify.com/album/{m.group(1)}", None
    except Exception as e:
        log.debug(f"Spotify album fetch fail: {e}")
    return None, None

def _album_from_yt_like(url: str, prefer_music: bool):
    p = urlparse(url)
    qs = parse_qs(p.query)
    lid = qs.get("list", [None])[0]
    if lid and lid.startswith("OLAK"):
        if prefer_music:
            return f"https://music.youtube.com/playlist?list={lid}", None
        else:
            return f"https://www.youtube.com/playlist?list={lid}", None
    return None, None

def _album_from_soundcloud(url: str):
    p = urlparse(url)
    inside = parse_qs(p.query).get("in", [None])[0]
    if inside:
        return f"https://soundcloud.com/{inside}", None
    return None, None

async def derive_album_buttons_all(links: dict):
    """Devuelve lista de botones de álbum [(texto, url)] para TODAS las plataformas que se puedan inferir."""
    buttons = []
    seen = set()
    for key in ["applemusic", "spotify", "youtubemusic", "youtube", "soundcloud"]:
        if key not in links:
            continue
        plat_url = links[key].get("url")
        if not plat_url:
            continue

        if key == "applemusic":
            album_url, _ = _album_from_apple(plat_url)
        elif key == "spotify":
            album_url, _ = await _album_from_spotify(plat_url)
        elif key == "youtubemusic":
            album_url, _ = _album_from_yt_like(plat_url, prefer_music=True)
        elif key == "youtube":
            album_url, _ = _album_from_yt_like(plat_url, prefer_music=False)
        elif key == "soundcloud":
            album_url, _ = _album_from_soundcloud(plat_url)
        else:
            album_url = None

        if album_url and album_url not in seen:
            seen.add(album_url)
            buttons.append((ALBUM_LABEL[key], album_url))
    return buttons

# almacenamiento para callbacks
STORE: dict[str, dict] = {}
ORDER = deque(maxlen=300)
def remember_links(links: dict, album_buttons: list[tuple[str, str]]) -> str:
    key = uuid.uuid4().hex
    STORE[key] = {"links": links, "albums": album_buttons}
    ORDER.append(key)
    while len(STORE) > ORDER.maxlen:
        old = ORDER.popleft()
        STORE.pop(old, None)
    return key

def build_keyboard(links: dict, show_all: bool, key: str, album_buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    sorted_keys = sort_keys(links)
    keys_to_show = sorted_keys if show_all else [k for k in sorted_keys if k.lower() in set(FAVS_LOWER)]

    botones = []

    # Filas de álbum: de 2 en 2 para que se lean bien
    if album_buttons:
        fila = []
        for text, url in album_buttons:
            fila.append(InlineKeyboardButton(text, url=url))
            if len(fila) == 2:
                botones.append(fila); fila = []
        if fila:
            botones.append(fila)

    # Plataformas (3 por fila)
    fila = []
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

    # Expandir/colapsar
    if not show_all and len(keys_to_show) < len(sorted_keys):
        botones.append([InlineKeyboardButton("Más opciones ▾", callback_data=f"more|{key}")])
    elif show_all:
        botones.append([InlineKeyboardButton("◀ Menos opciones", callback_data=f"less|{key}")])

    return InlineKeyboardMarkup(botones)

async def fetch_odesli(url: str):
    """Devuelve (links_by_platform, title, artist, cover_url)."""
    api = "https://api.song.link/v1-alpha.1/links"
    params = {"url": url, "userCountry": COUNTRY}
    headers = {"Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api, params=params, headers=headers, timeout=12)
        if r.status_code != 200:
            return None, None, None, None
        data = r.json()
        raw_links = data.get("linksByPlatform", {}) or {}
        links = regionalize_links(raw_links)
        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        return links or None, entity.get("title"), entity.get("artistName"), entity.get("thumbnailUrl")
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None, None, None, None

# -------- Chat handler --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = find_urls(update.message.text if update.message else "")
    if not urls:
        return
    for url in urls:
        if not is_music_url(url):
            continue

        links, title, artist, cover = await fetch_odesli(url)
        if not links:
            await update.message.reply_text("😕 busqué y busqué pero no encontré.")
            continue

        album_buttons = await derive_album_buttons_all(links)
        key = remember_links(links, album_buttons)
        keyboard = build_keyboard(links, show_all=False, key=key, album_buttons=album_buttons)

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

    album_buttons = await derive_album_buttons_all(links)
    key = remember_links(links, album_buttons)
    keyboard = build_keyboard(links, show_all=False, key=key, album_buttons=album_buttons)

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
    entry = STORE.get(key)
    if not entry:
        return

    links = entry["links"]
    album_buttons = entry.get("albums", [])
    show_all = data.startswith("more|")
    keyboard = build_keyboard(links, show_all=show_all, key=key, album_buttons=album_buttons)

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
