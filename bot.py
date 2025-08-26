# bot.py
import os
import re
import uuid
import logging
import asyncio
import unicodedata
from collections import deque
from urllib.parse import (
    urlparse, urlunparse, parse_qs, quote, unquote, quote_plus
)

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

# API keys para letras (opcional STANDS4; Musixmatch recomendado)
MUSIXMATCH_KEY = os.environ.get("MUSIXMATCH_KEY", "").strip()
STANDS4_UID = os.environ.get("STANDS4_UID", "").strip()
STANDS4_TOKENID = os.environ.get("STANDS4_TOKENID", "").strip()

# API key de setlist.fm (opcional si usas setlists)
SETLIST_FM_API_KEY = os.environ.get("SETLIST_FM_API_KEY", "").strip()
SETLIST_PAGE_SIZE = int(os.environ.get("SETLIST_PAGE_SIZE", "10"))
SETLIST_MAX_CONCURRENCY = int(os.environ.get("SETLIST_MAX_CONCURRENCY", "5"))

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
SETLIST_DOMAIN = "setlist.fm"

def is_music_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in MUSIC_DOMAINS)
    except Exception:
        return False

def is_setlist_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        return SETLIST_DOMAIN in host and "/setlist/" in p.path.lower()
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

# ====== ARTISTAS: detección y teclado de búsqueda ======

def _apple_artist_slug_to_name(path: str) -> str | None:
    parts = [p for p in path.strip("/").split("/") if p]
    if "artist" in parts:
        try:
            i = parts.index("artist")
            if i + 1 < len(parts):
                slug = unquote(parts[i + 1])
                name = slug.replace("-", " ").strip()
                if name:
                    return name.title()
        except Exception:
            return None
    return None

async def _spotify_artist_name_from_oembed(url: str) -> str | None:
    oembed = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(oembed, timeout=10)
        if r.status_code == 200:
            title = (r.json() or {}).get("title")
            if title:
                return str(title).strip()
    except Exception as e:
        log.debug(f"Spotify oEmbed falló: {e}")
    return None

async def detect_artist(url: str) -> dict | None:
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if ("music.apple.com" in host or "itunes.apple.com" in host) and "/artist/" in p.path.lower():
            name = _apple_artist_slug_to_name(p.path)
            if name:
                return {"name": name, "platform": "apple"}
        if "spotify.com" in host and "/artist/" in p.path.lower():
            name = await _spotify_artist_name_from_oembed(url)
            if name:
                return {"name": name, "platform": "spotify"}
    except Exception as e:
        log.debug(f"detect_artist error: {e}")
    return None

def build_artist_search_keyboard(artist_name: str) -> InlineKeyboardMarkup:
    q = quote(artist_name)
    buttons = [
        [
            InlineKeyboardButton("Espotifai", url=f"https://open.spotify.com/search/{q}"),
            InlineKeyboardButton("Yutú", url=f"https://www.youtube.com/results?search_query={q}"),
            InlineKeyboardButton("Yutúmusic", url=f"https://music.youtube.com/search?q={q}"),
        ],
        [
            InlineKeyboardButton("Manzanita", url=f"https://music.apple.com/{COUNTRY.lower()}/search?term={q}"),
            InlineKeyboardButton("SounClou", url=f"https://soundcloud.com/search?q={q}"),
            InlineKeyboardButton("Bandcamp", url=f"https://bandcamp.com/search?q={q}&item_type=b"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

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
            return (f"https://music.youtube.com/playlist?list={pid}" if prefer_music
                    else f"https://www.youtube.com/playlist?list={pid}"), None
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

# ===== Teclado / memoria para canciones =====
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

# ============ Letras: normalización + fuentes ==============

def _normalize_unicode(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2014", " ").replace("\u2013", " ").replace("—", " ").replace("–", " ")
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    return " ".join(s.split())

def _clean_title(title: str) -> str:
    t = _normalize_unicode(title or "")
    t = re.sub(r'\s*\(feat[^\)]*\)', '', t, flags=re.I)
    t = re.sub(r'\s*-\s*(official|audio|video|lyrics?|remastered?|live.*)$', '', t, flags=re.I)
    t = re.sub(r'\s*\[[^\]]*\]\s*$', '', t)
    return t.strip()

def _clean_artist(artist: str) -> str:
    a = _normalize_unicode(artist or "")
    a = re.split(r'[,&/]|feat\.?', a, flags=re.I)[0]
    return a.strip()

# --- Musixmatch (track_share_url) ---
async def _musixmatch_share_url(artist: str, title: str) -> str | None:
    if not MUSIXMATCH_KEY:
        return None
    q_artist = _clean_artist(artist or "")
    q_title = _clean_title(title or "")
    if not q_title:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.musixmatch.com/ws/1.1/track.search",
                params={
                    "q_track": q_title,
                    "q_artist": q_artist,
                    "page_size": 1,
                    "s_track_rating": "desc",
                    "f_has_lyrics": 1,
                    "apikey": MUSIXMATCH_KEY,
                },
            )
        data = r.json()
        track_list = (data.get("message", {}).get("body", {}) or {}).get("track_list", [])
        if not track_list:
            return None
        track = track_list[0].get("track") or {}
        return track.get("track_share_url") or None
    except Exception:
        return None

# --- Lyrics.com vía STANDS4 (si hay credenciales) ---
async def _lyricscom_link(artist: str, title: str) -> str | None:
    q_artist = _clean_artist(artist or "")
    q_title = _clean_title(title or "")
    if not q_title:
        return None

    if STANDS4_UID and STANDS4_TOKENID:
        base = "https://www.stands4.com/services/v2/lyrics.php"
        params = {
            "uid": STANDS4_UID,
            "tokenid": STANDS4_TOKENID,
            "term": q_title,
            "artist": q_artist,
            "format": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(base, params=params)
            j = r.json() or {}
            results = j.get("result") or []
            if isinstance(results, list) and results:
                hit = results[0]
                link = (hit or {}).get("song-link")
                if link:
                    return link
        except Exception:
            pass
    return None

# --- Búsqueda DDG para otras webs de letras ---
DDG_HTML = "https://duckduckgo.com/html/?q={q}"

async def _ddg_first_result(site: str, artist: str, title: str) -> str | None:
    """Busca en DuckDuckGo (HTML) el primer resultado para site:<site> "title" "artist" lyrics."""
    q_title = _clean_title(title or "")
    q_artist = _clean_artist(artist or "")
    if not q_title:
        return None
    query = f'site:{site} "{q_title}" "{q_artist}" lyrics'
    url = DDG_HTML.format(q=quote_plus(query))
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
        html = r.text or ""
        # Enlaces típicos de resultados de DDG HTML:
        # <a rel="nofollow" class="result__a" href="https://..."> ...
        m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html)
        if not m:
            # Fallback: algunos temas usan result__url
            m = re.search(r'<a[^>]+class="result__url"[^>]+href="([^"]+)"', html)
        if not m:
            return None
        link = m.group(1)
        # Sencilla validación de dominio correcto
        if site not in link:
            return None
        return link
    except Exception:
        return None

async def get_lyrics_links(artist: str | None, title: str | None) -> dict | None:
    """
    Devuelve dict con enlaces presentes solo si hay match en al menos uno:
      {"lyricscom": url|None, "musixmatch": url|None, "letras": url|None, "azlyrics": url|None, "genius": url|None}
    """
    artist = artist or ""
    title = title or ""
    if not title.strip():
        return None

    # Primero fuentes "directas"
    mm, lc = await asyncio.gather(
        _musixmatch_share_url(artist, title),
        _lyricscom_link(artist, title),
    )

    # Luego búsquedas en letras.com / azlyrics / genius
    letras, az, genius = await asyncio.gather(
        _ddg_first_result("www.letras.com", artist, title),
        _ddg_first_result("www.azlyrics.com", artist, title),
        _ddg_first_result("genius.com", artist, title),
    )

    if any([mm, lc, letras, az, genius]):
        return {"lyricscom": lc, "musixmatch": mm, "letras": letras, "azlyrics": az, "genius": genius}
    return None

# =================== Teclados ===================

def build_keyboard(links: dict, show_all: bool, key: str, album_buttons: list[tuple[str, str]], lyrics_links: dict | None = None) -> InlineKeyboardMarkup:
    sorted_keys = sort_keys(links)
    fav_set = set(FAVS_LOWER)
    keys_to_show = sorted_keys if show_all else [k for k in sorted_keys if k.lower() in fav_set]

    botones = []

    # 1) Canción (plataformas)
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

    # 1.5) Letras (solo si hay coincidencias)
    if lyrics_links:
        lyr_row = []
        if lyrics_links.get("lyricscom"):
            lyr_row.append(InlineKeyboardButton("📝 Lyrics.com", url=lyrics_links["lyricscom"]))
        if lyrics_links.get("musixmatch"):
            lyr_row.append(InlineKeyboardButton("🎼 Musixmatch", url=lyrics_links["musixmatch"]))
        if lyrics_links.get("letras"):
            lyr_row.append(InlineKeyboardButton("🇪🇸 Letras.com", url=lyrics_links["letras"]))
        if lyrics_links.get("azlyrics"):
            lyr_row.append(InlineKeyboardButton("AZLyrics", url=lyrics_links["azlyrics"]))
        if lyrics_links.get("genius"):
            lyr_row.append(InlineKeyboardButton("Genius", url=lyrics_links["genius"]))
        if lyr_row:
            # Partimos en filas de 3 para no romper el layout
            for i in range(0, len(lyr_row), 3):
                botones.append(lyr_row[i:i+3])

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

# ===== Odesli (canciones) =====
async def fetch_odesli(url: str):
    """
    Devuelve: (links_for_track, title, artist_name, thumbnail_url, page_url)
    """
    api = "https://api.song.link/v1-alpha.1/links"
    params = {"url": url, "userCountry": COUNTRY}
    headers = {"Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api, params=params, headers=headers, timeout=12)
        if r.status_code != 200:
            return None, None, None, None, None
        data = r.json()
        raw_links = (data.get("linksByPlatform", {}) or {})
        links_norm = normalize_links(raw_links)
        links_for_track = regionalize_links_for_track(links_norm)

        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        title = entity.get("title")
        artist = entity.get("artistName")
        thumb = entity.get("thumbnailUrl")
        page_url = data.get("pageUrl") or data.get("pageUrlShort") or data.get("url")

        return links_for_track or None, title, artist, thumb, page_url
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None, None, None, None, None

# ======== SETLIST.FM ========
SETLIST_CACHE: dict[str, dict] = {}  # setlistId -> {'meta':..., 'songs':[...]}

def _extract_setlist_id(url: str) -> str | None:
    try:
        p = urlparse(url)
        m = re.search(r"([0-9a-f]{8})(?:\.html)?$", p.path.lower())
        return m.group(1) if m else None
    except Exception:
        return None

async def fetch_setlist_json(setlist_id: str) -> dict | None:
    if not SETLIST_FM_API_KEY:
        log.warning("Falta SETLIST_FM_API_KEY")
        return None
    url = f"https://api.setlist.fm/rest/1.0/setlist/{setlist_id}"
    headers = {
        "x-api-key": SETLIST_FM_API_KEY,
        "Accept": "application/json",
        "Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8",
        "User-Agent": "setlist-resolver-bot/1.0",
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        log.warning(f"setlist.fm {setlist_id} -> {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log.warning(f"Error consultando setlist.fm: {e}")
    return None

def _ensure_list(x):
    if not x:
        return []
    if isinstance(x, list):
        return x
    return [x]

def parse_setlist_songs(js: dict) -> tuple[dict, list[dict]]:
    if not js:
        return {}, []
    artist = (js.get("artist") or {}).get("name")
    venue = (js.get("venue") or {}).get("name")
    city = ((js.get("venue") or {}).get("city") or {}).get("name")
    country = (((js.get("venue") or {}).get("city") or {}).get("country") or {}).get("code")
    event_date = js.get("eventDate")  # dd-mm-yyyy
    url = js.get("url") or ""
    sets = ((js.get("sets") or {}).get("set")) or []
    sets = _ensure_list(sets)

    songs = []
    for s in sets:
        items = _ensure_list(s.get("song"))
        for it in items:
            title = (it or {}).get("name")
            if not title:
                continue
            is_tape = bool((it or {}).get("tape"))
            cover = ((it or {}).get("cover") or {}).get("name")
            songs.append({"title": title, "is_tape": is_tape, "cover": cover})

    meta = {
        "artist": artist, "venue": venue, "city": city,
        "country": country, "eventDate": event_date, "url": url
    }
    return meta, songs

async def apple_search_track_url(artist: str, title: str) -> str | None:
    term = f"{artist} {title}".strip()
    params = {"term": term, "entity": "song", "limit": 1, "country": COUNTRY, "media": "music"}
    url = "https://itunes.apple.com/search"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params=params, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        track_url = results[0].get("trackViewUrl")
        if not track_url:
            return None
        return _regionalize_apple(track_url, for_album=False)
    except Exception as e:
        log.debug(f"Apple search falló para '{term}': {e}")
        return None

async def resolve_song_links(artist: str, title: str) -> tuple[dict | None, str | None]:
    seed = await apple_search_track_url(artist, title)
    if not seed:
        return None, None
    links, t, a, thumb, page_url = await fetch_odesli(seed)
    return links, page_url

def _pick_youtube_key(links: dict) -> str | None:
    if "youtubemusic" in links:
        return "youtubemusic"
    if "youtube" in links:
        return "youtube"
    return None

# Memoria temporal para teclados de setlist
SETLIST_STORE: dict[str, dict] = {}
SETLIST_ORDER = deque(maxlen=120)

def remember_setlist(setlist_id: str, meta: dict, items: list[dict]) -> str:
    key = uuid.uuid4().hex
    SETLIST_STORE[key] = {"setlist_id": setlist_id, "meta": meta, "items": items}
    SETLIST_ORDER.append(key)
    while len(SETLIST_STORE) > SETLIST_ORDER.maxlen:
        old = SETLIST_ORDER.popleft()
        SETLIST_STORE.pop(old, None)
    return key

def _format_song_label(idx: int, title: str, cover: str | None) -> str:
    base = f"{idx}. {title}"
    if cover:
        return f"{base} (cover de {cover})"
    return base

def build_setlist_keyboard(key: str, page: int) -> InlineKeyboardMarkup:
    entry = SETLIST_STORE.get(key)
    if not entry:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Expiró", callback_data="noop|x")]])

    items = entry["items"]
    total = len(items)
    pages = max(1, (total + SETLIST_PAGE_SIZE - 1) // SETLIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * SETLIST_PAGE_SIZE
    chunk = items[start:start + SETLIST_PAGE_SIZE]

    botones: list[list[InlineKeyboardButton]] = []

    url = (entry.get("meta") or {}).get("url")
    if url:
        botones.append([InlineKeyboardButton("📄 Ver en setlist.fm", url=url)])

    for i, it in enumerate(chunk, start=start + 1):
        title = _format_song_label(i, it.get("title") or "", it.get("cover"))
        botones.append([InlineKeyboardButton(title[:64], callback_data=f"noop|{key}")])

        links = it.get("links") or {}
        page_url = it.get("page_url")

        fila = []
        if "spotify" in links and (links["spotify"].get("url")):
            fila.append(InlineKeyboardButton("Espotifai", url=links["spotify"]["url"]))
        yk = _pick_youtube_key(links)
        if yk and links.get(yk, {}).get("url"):
            fila.append(InlineKeyboardButton(nice_name(yk), url=links[yk]["url"]))
        if "applemusic" in links and (links["applemusic"].get("url")):
            fila.append(InlineKeyboardButton("Manzanita", url=links["applemusic"]["url"]))

        if page_url:
            fila.append(InlineKeyboardButton("⋯ Más", url=page_url))
        else:
            q = quote_plus(f"{(entry['meta'] or {}).get('artist','')} {it.get('title','')}".strip())
            fila.append(InlineKeyboardButton("⋯ Buscar", url=f"https://song.link/search?q={q}"))

        if fila:
            botones.append(fila)

    if pages > 1:
        prev_page = max(0, page - 1)
        next_page = min(pages - 1, page + 1)
        botones.append([
            InlineKeyboardButton("◀", callback_data=f"slp|{key}|{prev_page}"),
            InlineKeyboardButton(f"Página {page+1}/{pages}", callback_data=f"noop|{key}"),
            InlineKeyboardButton("▶", callback_data=f"slp|{key}|{next_page}"),
        ])

    return InlineKeyboardMarkup(botones)

async def handle_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    setlist_id = _extract_setlist_id(url)
    if not setlist_id:
        await update.message.reply_text("No pude leer el ID del setlist en esa URL.")
        return
    if not SETLIST_FM_API_KEY:
        await update.message.reply_text("Falta SETLIST_FM_API_KEY en el entorno para usar setlist.fm.")
        return

    cached = SETLIST_CACHE.get(setlist_id)
    if not cached:
        js = await fetch_setlist_json(setlist_id)
        if not js:
            await update.message.reply_text("No pude obtener ese setlist (API). Intenta más tarde.")
            return
        meta, songs_raw = parse_setlist_songs(js)
        songs_raw = [s for s in songs_raw if not s.get("is_tape")]
        cached = {"meta": meta, "songs_raw": songs_raw}
        SETLIST_CACHE[setlist_id] = cached

    artist_show = (cached["meta"] or {}).get("artist") or ""
    songs_raw = cached["songs_raw"]

    sem = asyncio.Semaphore(SETLIST_MAX_CONCURRENCY)
    resolved: list[dict] = []

    async def _resolve_one(s):
        title = s.get("title") or ""
        cover = s.get("cover")
        async with sem:
            links, page_url = await resolve_song_links(artist_show, title)
        resolved.append({"title": title, "cover": cover, "links": links or {}, "page_url": page_url})

    await asyncio.gather(*[_resolve_one(s) for s in songs_raw])

    key = remember_setlist(setlist_id, cached["meta"], resolved)

    meta = cached["meta"] or {}
    cap_parts = []
    if meta.get("artist"):
        cap_parts.append(f"🎤 {meta['artist']}")
    venue_city = " — ".join([x for x in [meta.get("venue"), meta.get("city")] if x])
    if venue_city:
        cap_parts.append(venue_city)
    if meta.get("eventDate"):
        cap_parts.append(meta["eventDate"])
    header = " | ".join(cap_parts) if cap_parts else "Setlist"

    caption = f"{header}\n📃 {len(resolved)} canciones\n\nSelecciona una canción y elige plataforma:"
    keyboard = build_setlist_keyboard(key, page=0)

    await update.message.reply_text(caption, reply_markup=keyboard)

# -------- Chat handler --------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else ""
    urls = find_urls(text)
    if not urls:
        return
    for url in urls:
        if is_setlist_url(url):
            await handle_setlist(update, context, url)
            continue

        if is_music_url(url):
            artist = await detect_artist(url)
            if artist:
                name = artist["name"]
                caption = f"🧑‍🎤 {name}\n🔎 Búscalo en:"
                kb = build_artist_search_keyboard(name)
                await update.message.reply_text(caption, reply_markup=kb)
                continue

            links, title, artist_name, cover, _page = await fetch_odesli(url)
            if not links:
                continue

            # Letras robustas (lyrics.com, musixmatch, letras, azlyrics, genius)
            lyrics_links = await get_lyrics_links(artist_name or "", title or "")

            album_buttons = await derive_album_buttons_all(links)
            key = remember_links(links, album_buttons)
            keyboard = build_keyboard(
                links, show_all=False, key=key, album_buttons=album_buttons, lyrics_links=lyrics_links
            )

            caption = "🎶 Disponible en:"
            if title and artist_name:
                caption = f"🎵 {title} — {artist_name}\n🎶 Disponible en:"
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

    if is_setlist_url(url):
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    if not is_music_url(url):
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    artist = await detect_artist(url)
    if artist:
        name = artist["name"]
        caption = f"🧑‍🎤 {name}\n🔎 Búscalo en:"
        kb = build_artist_search_keyboard(name)
        rid = str(uuid.uuid4())
        results = [InlineQueryResultArticle(
            id=rid, title=f"Artista: {name}",
            input_message_content=InputTextMessageContent(caption),
            reply_markup=kb, description="Buscar al artista en otras plataformas"
        )]
        await update.inline_query.answer(results, cache_time=10, is_personal=True)
        return

    links, title, artist_name, cover, _page = await fetch_odesli(url)
    if not links:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    lyrics_links = await get_lyrics_links(artist_name or "", title or "")

    album_buttons = await derive_album_buttons_all(links)
    key = remember_links(links, album_buttons)
    keyboard = build_keyboard(
        links, show_all=False, key=key, album_buttons=album_buttons, lyrics_links=lyrics_links
    )

    caption = "🎶 Disponible en:"
    if title and artist_name:
        caption = f"🎵 {title} — {artist_name}\n🎶 Disponible en:"
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

    if data.startswith("slp|"):
        _, key, page_s = data.split("|", 3)
        try:
            page = int(page_s)
        except Exception:
            page = 0
        keyboard = build_setlist_keyboard(key, page=page)
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
            log.warning(f"No pude editar teclado (setlist): {e}")
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

    keyboard = build_keyboard(links, show_all=show_all, key=key, album_buttons=album_buttons, lyrics_links=None)

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
    tg = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg.add_handler(InlineQueryHandler(handle_inline_query))
    tg.add_handler(CallbackQueryHandler(callbacks))

    await start_health_server()
    log.info("✅ Iniciando en modo POLLING…")
    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
