# bot.py
import os
import re
import uuid
import logging
import asyncio
import html
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
        html_src = r.text
        m = re.search(r'"playlistId":"(OLAK[^"]+)"', html_src) or re.search(r'list=(OLAK[^"&]+)', html_src)
        if m:
            pid = m.group(1)
            if prefer_music:
                return f"https://music.youtube.com/playlist?list={pid}", None
            else:
                return f"https://www.youtube.com/playlist?list={pid}", None
        m = re.search(r'"browseId":"(MPREb[^"]+)"', html_src) or re.search(r'/browse/(MPREb[^"?]+)', html_src)
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

# ===== Odesli (pistas) =====
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

# ===== Artistas: detección + extracción de nombre + búsquedas =====
def is_artist_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        path = p.path.lower()
        if ("music.apple.com" in host or "itunes.apple.com" in host) and "/artist/" in path:
            return True
        if ("open.spotify.com" in host) and "/artist/" in path:
            return True
        return False
    except Exception:
        return False

def _clean_candidate(text: str) -> str:
    # elimina cosas como "en Apple Music", "en Spotify", etc.
    text = re.sub(r"\s+en\s+(apple\s*music|spotify|youtube(\s*music)?|soundcloud).*", "", text, flags=re.I)
    text = re.sub(r"\s*\(.*?\)\s*$", "", text)  # paréntesis finales
    return text.strip()

def _artist_from_apple_slug(url: str) -> str | None:
    # Apple suele tener /artist/<slug>/<id>
    try:
        p = urlparse(url)
        parts = [s for s in p.path.split("/") if s]
        if "artist" in parts:
            i = parts.index("artist")
            if i + 1 < len(parts):
                slug = unquote(parts[i + 1])
                slug = slug.replace("-", " ").strip()
                if slug and not slug.isnumeric():
                    # Normaliza mayúsculas (pero respeta acrónimos)
                    return slug.title()
    except Exception:
        pass
    return None

async def _artist_from_page_title(url: str) -> str | None:
    # Plan C: rascar <meta property="og:title" ...> o <title>
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=10)
        html_src = r.text or ""
        # og:title primero
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_src, re.I)
        if not m:
            m = re.search(r"<title>([^<]+)</title>", html_src, re.I)
        if m:
            raw = html.unescape(m.group(1)).strip()
            # ejemplos: "Gorillaz en Apple Music", "Gorillaz | Spotify"
            raw = re.sub(r"\s*\|\s*Spotify.*$", "", raw, flags=re.I)
            raw = _clean_candidate(raw)
            # corta añadidos típicos
            raw = re.sub(r"\s+on\s+Apple\s*Music.*$", "", raw, flags=re.I)
            if raw:
                return raw
    except Exception as e:
        log.debug(f"No pude extraer título de página: {e}")
    return None

async def extract_artist_name(url: str, message_text: str) -> str | None:
    # A) Apple: usa slug
    name = _artist_from_apple_slug(url)
    if name:
        return name

    # B) Texto alrededor: quita el URL y usa lo restante si parece un nombre
    if message_text:
        text = message_text.replace(url, " ").strip()
        text = _clean_candidate(text)
        # si quedó algo "humano"
        if 1 <= len(text) <= 80 and not URL_RE.search(text):
            return text

    # C) Rascar título/og:title
    name = await _artist_from_page_title(url)
    return name

def build_artist_search_links(artist: str) -> dict:
    q = artist.strip()
    if not q:
        return {}
    # No encodamos con quote_plus porque la mayoría acepta espacios; agregamos simple escape
    query = q.replace("#", "")
    return {
        "spotify": {"url": f"https://open.spotify.com/search/{query}"},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "applemusic": {"url": f"https://music.apple.com/search?term={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search?q={query}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={query}"},
    }

def build_artist_keyboard(links: dict) -> InlineKeyboardMarkup:
    # muestra favoritos primero (spotify, youtube, ytm, applemusic, soundcloud) y luego bandcamp
    order = sort_keys(links)
    botones, fila = [], []
    for k in order:
        url = links[k]["url"]
        fila.append(InlineKeyboardButton(nice_name(k), url=url))
        if len(fila) == 3:
            botones.append(fila); fila = []
    if fila:
        botones.append(fila)
    return InlineKeyboardMarkup(botones)

# -------- Chat handler --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else ""
    urls = find_urls(text)
    if not urls:
        return
    for url in urls:
        if not is_music_url(url):
            continue

        # 1) ¿Es ARTISTA?
        if is_artist_url(url):
            artist = await extract_artist_name(url, text)
            if not artist:
                # como último recurso, usa palabra "Artista"
                artist = "Artista"
            search_links = build_artist_search_links(artist)
            caption = f"🧑‍🎤 {artist}\n🔎 Búscalo en:"
            kb = build_artist_keyboard(search_links)
            await update.message.reply_text(caption, reply_markup=kb)
            continue

        # 2) PISTA / ÁLBUM vía Odesli
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

    # Artista en inline
    if is_artist_url(url):
        artist = await extract_artist_name(url, q) or "Artista"
        search_links = build_artist_search_links(artist)
        kb = build_artist_keyboard(search_links)
        caption = f"🧑‍🎤 {artist}\n🔎 Búscalo en:"
        rid = str(uuid.uuid4())
        results = [InlineQueryResultArticle(
            id=rid, title=f"Buscar {artist}",
            input_message_content=InputTextMessageContent(caption),
            reply_markup=kb, description="Recomendar artista en otras plataformas"
        )]
        await update.inline_query.answer(results, cache_time=10, is_personal=True)
        return

    # Pista/álbum en inline
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
