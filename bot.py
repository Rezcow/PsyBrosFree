# bot.py
import os
import re
import uuid
import logging
import asyncio
from collections import deque
from urllib.parse import urlparse, urlunparse, parse_qs, unquote

import httpx
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent,
)
from telegram.ext import (
    Application, MessageHandler, ContextTypes, filters,
    InlineQueryHandler, CallbackQueryHandler,
)

# -------- Config / Logging --------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("odesli-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL").upper()
PORT = int(os.environ.get("PORT", "8000"))  # Render provee PORT

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
            seen.add(u)
            out.append(u)
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
def _ensure_region_path(path: str) -> str:
    parts = path.strip("/").split("/", 1)
    if parts and len(parts[0]) == 2:
        parts[0] = COUNTRY.lower()
        return "/" + "/".join(parts)
    return f"/{COUNTRY.lower()}/{path.strip('/')}"

def _regionalize_apple(url: str, for_album: bool = False) -> str:
    try:
        p = urlparse(url)
        host = "music.apple.com"
        new_path = _ensure_region_path(p.path)
        if for_album:
            return urlunparse((p.scheme, host, new_path, "", "", ""))
        else:
            return urlunparse((p.scheme, host, new_path, p.params, p.query, p.fragment))
    except Exception as e:
        log.debug(f"No pude regionalizar Apple Music: {e}")
        return url

def normalize_links(raw_links: dict) -> dict:
    out = {}
    for k, info in raw_links.items():
        out[k.lower()] = info
    return out

def regionalize_links_for_track(links: dict) -> dict:
    out = {}
    for k, info in links.items():
        url = info.get("url")
        if not url:
            continue
        if k in ("applemusic", "itunes"):
            url = _regionalize_apple(url, for_album=False)
        out[k] = {**info, "url": url}
    return out

# ===== Derivar enlaces de ÁLBUM =====
ALBUM_LABEL = {
    "applemusic": "💿🍎",
    "spotify": "💿🎧",
    "youtubemusic": "💿🎵",
    "youtube": "💿▶️",
    "soundcloud": "💿☁️",
}

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

def _album_from_apple(url: str):
    return _regionalize_apple(url, for_album=True), None

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

async def _ytm_album_from_page(url: str, prefer_music: bool = True):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=12)
        html = r.text
        m = re.search(r'"playlistId":"(OLAK[^"]+)"', html) or re.search(r'list=(OLAK[^"&]+)', html)
        if m:
            pid = m.group(1)
            if prefer_music:
                return f"https://music.youtube.com/playlist?list={pid}", None
            else:
                return f"https://www.youtube.com/playlist?list={pid}", None
        m = re.search(r'"browseId":"(MPREb[^"]+)"', html) or re.search(r'/browse/(MPREb[^"?]+)', html)
        if m:
            bid = m.group(1)
            return f"https://music.youtube.com/browse/{bid}", None
    except Exception as e:
        log.debug(f"YT scrape fail: {e}")
    return None, None

async def _album_from_youtube_robust(url: str, prefer_music: bool):
    album_url, _ = _album_from_yt_like(url, prefer_music)
    if album_url:
        return album_url, None
    return await _ytm_album_from_page(url, prefer_music)

async def _album_from_soundcloud(url: str):
    p = urlparse(url)
    inside = parse_qs(p.query).get("in", [None])[0]
    if inside:
        return f"https://soundcloud.com/{inside}", None
    return None, None

async def derive_album_buttons_all(links: dict):
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
            album_url, _ = await _album_from_youtube_robust(plat_url, prefer_music=True)
        elif key == "youtube":
            album_url, _ = await _album_from_youtube_robust(plat_url, prefer_music=False)
        elif key == "soundcloud":
            album_url, _ = await _album_from_soundcloud(plat_url)
        else:
            album_url = None

        if album_url and album_url not in seen:
            seen.add(album_url)
            label = ALBUM_LABEL.get(key, "💿")
            buttons.append((label, album_url))
    return buttons

# ===== Teclado / memoria =====
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
    fav_set = set(FAVS_LOWER)
    keys_to_show = sorted_keys if show_all else [k for k in sorted_keys if k.lower() in fav_set]

    botones = []

    # 1) Canción
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

    # 2) Álbum
    if album_buttons:
        botones.append([InlineKeyboardButton("💿 Álbum", callback_data=f"noop|{key}")])
        fila = []
        for text, url in album_buttons:
            fila.append(InlineKeyboardButton(text, url=url))
            if len(fila) == 3:
                botones.append(fila); fila = []
        if fila:
            botones.append(fila)

    # 3) Expandir/colapsar
    if not show_all and len(keys_to_show) < len(sorted_keys):
        botones.append([InlineKeyboardButton("Más opciones ▾", callback_data=f"more|{key}")])
    elif show_all:
        botones.append([InlineKeyboardButton("◀ Menos opciones", callback_data=f"less|{key}")])

    return InlineKeyboardMarkup(botones)

# ===== Odesli =====
async def fetch_odesli(url: str):
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
        links_norm = normalize_links(raw_links)
        links_for_track = regionalize_links_for_track(links_norm)

        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        return links_for_track or None, entity.get("title"), entity.get("artistName"), entity.get("thumbnailUrl")
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None, None, None, None

# ======== ARTIST FALLBACK (BÚSQUEDAS) ========
_ARTIST_HOST_KEYS = (
    "music.apple.com", "itunes.apple.com", "geo.music.apple.com",
    "open.spotify.com", "spotify.com",
    "music.youtube.com", "youtube.com", "youtu.be",
    "soundcloud.com", "bandcamp.com", "deezer.com", "tidal.com",
)

def _looks_like_artist_path(host: str, path: str) -> bool:
    host = host.lower()
    path = path.lower()
    if "apple.com" in host and "/artist/" in path:
        return True
    if "spotify.com" in host and "/artist/" in path:
        return True
    # YouTube Music canales/artistas: /channel/ o /browse/
    if "music.youtube.com" in host and ("/channel/" in path or "/browse/" in path):
        return True
    # SoundCloud perfiles: /<usuario> (sin /track/ ni /sets/)
    if "soundcloud.com" in host and path.count("/") == 2 and not any(seg in path for seg in ("/track/", "/sets/", "/mixes/")):
        return True
    return False

def _artist_name_from_url(url: str) -> str | None:
    """
    Intenta extraer el nombre del artista desde la URL (sin pedir HTML).
    - Apple Music: /artist/<slug>/<id>
    - SoundCloud:  /<user>
    Para Spotify intentaremos scrape rápido del <title> si es necesario (más abajo).
    """
    try:
        p = urlparse(url)
        host, path = p.netloc.lower(), unquote(p.path)
        parts = [seg for seg in path.split("/") if seg]

        # Apple Music
        if "apple.com" in host and "artist" in parts:
            i = parts.index("artist")
            if i + 1 < len(parts):
                slug = parts[i+1]
                # "soulacybin" -> "Soulacybin" | "red-hot-chili-peppers" -> "Red Hot Chili Peppers"
                name = " ".join(s.capitalize() for s in slug.replace("_", "-").split("-"))
                return name

        # SoundCloud perfil raíz: /usuario
        if "soundcloud.com" in host and len(parts) == 1:
            return " ".join(s.capitalize() for s in parts[0].replace("-", " ").split())

        # Bandcamp: <usuario>.bandcamp.com
        if host.endswith(".bandcamp.com"):
            user = host.split(".bandcamp.com")[0]
            return " ".join(s.capitalize() for s in user.replace("-", " ").split())

        # Spotify: no viene el nombre en la URL
        return None
    except Exception:
        return None

async def _try_fetch_page_title(url: str) -> str | None:
    """
    Intenta traer el <title> y recortar “ | Spotify”, “ – Apple Music”, etc.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=8)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, flags=re.I|re.S)
        if not m:
            return None
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        # Recortes típicos
        title = re.sub(r"\s*\|\s*Spotify.*$", "", title, flags=re.I)
        title = re.sub(r"\s*en\s*Apple Music.*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*YouTube.*$", "", title, flags=re.I)
        title = re.sub(r"\s*on\s*SoundCloud.*$", "", title, flags=re.I)
        # Evitar cosas tipo “Artist – Topic”
        title = re.sub(r"\s*–\s*Topic$", "", title, flags=re.I)
        return title
    except Exception as e:
        log.debug(f"No pude leer título de página: {e}")
        return None

def _build_artist_search_links(artist: str) -> dict:
    q = artist.strip()
    return {
        "spotify": {"url": f"https://open.spotify.com/search/{q}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={q}"},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={q}"},
        "applemusic": {"url": f"https://music.apple.com/search?term={q}"},
        "soundcloud": {"url": f"https://soundcloud.com/search/people?q={q}"},
        "deezer": {"url": f"https://www.deezer.com/search/{q}"},
        "tidal": {"url": f"https://tidal.com/search?q={q}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={q}&item_type=b"},
    }

def _artist_keyboard(artist: str) -> InlineKeyboardMarkup:
    links = _build_artist_search_links(artist)
    sorted_keys = sort_keys(links)
    botones, fila = [], []
    for k in sorted_keys:
        url = links[k]["url"]
        fila.append(InlineKeyboardButton(text=nice_name(k), url=url))
        if len(fila) == 3:
            botones.append(fila); fila = []
    if fila:
        botones.append(fila)
    return InlineKeyboardMarkup(botones)

async def maybe_handle_artist_url(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Si la URL luce como “de artista”, responde con búsquedas del artista en otras plataformas.
    Devuelve True si manejó el mensaje.
    """
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path
    if not any(h in host for h in _ARTIST_HOST_KEYS):
        return False
    if not _looks_like_artist_path(host, path):
        return False

    # 1) Intentar deducir nombre sin red
    artist = _artist_name_from_url(url)

    # 2) Si no se pudo (p.ej. Spotify), intentar título de la página
    if not artist and ("spotify.com" in host or "youtube" in host):
        artist = await _try_fetch_page_title(url)

    if not artist:
        # Último recurso: usar host como hint
        artist = "Artista"

    caption = f"👤 {artist}\n🔎 Búscalo en:"
    keyboard = _artist_keyboard(artist)
    try:
        await update.message.reply_text(caption, reply_markup=keyboard)
    except Exception:
        # Inline (por si vino desde otro origen)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=keyboard)
    return True

# -------- Chat handler --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = find_urls(update.message.text if update.message else "")
    if not urls:
        return
    for url in urls:
        if not is_music_url(url):
            continue

        # --- NUEVO: Fallback para enlaces de ARTISTAS ---
        handled = await maybe_handle_artist_url(url, update, context)
        if handled:
            continue

        # Tracks / playlists / álbumes -> Odesli
        links, title, artist, cover = await fetch_odesli(url)
        if not links:
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

    # --- NUEVO: Artista en inline ---
    p = urlparse(url)
    if _looks_like_artist_path(p.netloc.lower(), p.path):
        artist = _artist_name_from_url(url) or (await _try_fetch_page_title(url)) or "Artista"
        caption = f"👤 {artist}\n🔎 Búscalo en:"
        keyboard = _artist_keyboard(artist)
        rid = str(uuid.uuid4())
        results = [InlineQueryResultArticle(
            id=rid, title=f"{artist} — plataformas",
            input_message_content=InputTextMessageContent(caption),
            reply_markup=keyboard, description="Buscar al artista en otras plataformas"
        )]
        await update.inline_query.answer(results, cache_time=10, is_personal=True)
        return

    # Tracks/álbumes/playlists normales
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

# -------- Callbacks --------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    data = cq.data or ""
    if data.startswith("noop|"):
        return
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

# ---- Mini servidor HTTP para keep-alive
async def health_handler(request):
    return web.Response(text="ok")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/healthz", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server listo en :{PORT}/healthz")

async def main():
    # Telegram app
    tg = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg.add_handler(InlineQueryHandler(handle_inline_query))
    tg.add_handler(CallbackQueryHandler(callbacks))

    # Arranca health server y polling en paralelo
    await start_health_server()
    log.info("✅ Iniciando en modo POLLING…")
    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=True)

    # Mantener proceso vivo
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
