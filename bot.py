import os
import re
import uuid
import html
import json
import time
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
log = logging.getLogger("psybros-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL").upper()
PORT = int(os.environ.get("PORT", "8000"))

# API keys opcionales
MUSIXMATCH_KEY = os.environ.get("MUSIXMATCH_KEY", "").strip()
STANDS4_UID = os.environ.get("STANDS4_UID", "").strip()
STANDS4_TOKENID = os.environ.get("STANDS4_TOKENID", "").strip()

# setlist.fm
SETLIST_FM_API_KEY = os.environ.get("SETLIST_FM_API_KEY", "").strip()
SETLIST_PAGE_SIZE = int(os.environ.get("SETLIST_PAGE_SIZE", "10"))
SETLIST_MAX_CONCURRENCY = int(os.environ.get("SETLIST_MAX_CONCURRENCY", "5"))

# Odesli now optional; keep very conservative settings
ODESLI_MAX_CONCURRENCY = int(os.environ.get("ODESLI_MAX_CONCURRENCY", "1"))
ODESLI_MAX_RETRIES = int(os.environ.get("ODESLI_MAX_RETRIES", "2"))
ODESLI_CACHE_TTL = int(os.environ.get("ODESLI_CACHE_TTL", "21600"))  # 6 horas
GENERIC_CACHE_TTL = int(os.environ.get("GENERIC_CACHE_TTL", "21600"))  # 6 horas
LYRICS_CACHE_TTL = int(os.environ.get("LYRICS_CACHE_TTL", "43200"))  # 12 horas
SETLIST_CACHE_TTL = int(os.environ.get("SETLIST_CACHE_TTL", "86400"))  # 24 horas
SPOTIFY_CACHE_TTL = int(os.environ.get("SPOTIFY_CACHE_TTL", "21600"))

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


def now_ts() -> float:
    return time.time()


def ttl_get(cache: dict, key: str):
    item = cache.get(key)
    if not item:
        return None
    expires_at, value = item
    if expires_at < now_ts():
        cache.pop(key, None)
        return None
    return value


def ttl_set(cache: dict, key: str, value, ttl: int):
    cache[key] = (now_ts() + ttl, value)


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
        path = p.path.lower()
        return "setlist.fm" in host and "/setlist/" in path
    except Exception:
        return False


def is_spotify_url(url: str) -> bool:
    try:
        return "spotify.com" in urlparse(url).netloc.lower()
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
    if k == "spotify":
        return "Espotifai"
    if k == "youtube":
        return "Yutú"
    if k == "youtubemusic":
        return "Yutúmusic"
    if k == "applemusic":
        return "Manzanita"
    if k == "soundcloud":
        return "SounClou"
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


def normalize_music_url(url: str) -> str:
    """
    Normaliza URLs musicales para evitar duplicar consultas por variantes
    equivalentes, especialmente Spotify /intl-es.
    """
    try:
        p = urlparse(url)
        scheme = p.scheme or "https"
        netloc = p.netloc
        path = p.path or ""
        fragment = ""

        if "spotify.com" in netloc.lower():
            path = re.sub(r"^/intl-[a-z]{2}(?=/)", "", path, flags=re.I)
            path = re.sub(r"^/intl-[a-z]{2}-[a-z]{2}(?=/)", "", path, flags=re.I)
            return urlunparse((scheme, netloc, path, "", "", fragment))

        return urlunparse((scheme, netloc, path, p.params, p.query, fragment))
    except Exception:
        return url


# ---- Regionalización Apple Music ----
def _ensure_region_path(path: str) -> str:
    parts = path.strip("/").split("/", 1)
    if parts and len(parts[0]) == 2:
        parts[0] = COUNTRY.lower()
        return "/" + "/".join(parts)
    return f"/{COUNTRY.lower()}/{path.strip('/')}"


def _regionalize_apple(url: str | None, for_album: bool = False) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
        host = "music.apple.com"
        new_path = _ensure_region_path(p.path)
        if for_album:
            return urlunparse((p.scheme or "https", host, new_path, "", "", ""))
        return urlunparse((p.scheme or "https", host, new_path, p.params, p.query, p.fragment))
    except Exception as e:
        log.debug(f"No pude regionalizar Apple Music: {e}")
        return url


def normalize_links(raw_links: dict) -> dict:
    return {k.lower(): v for k, v in (raw_links or {}).items()}


def regionalize_links_for_track(links: dict) -> dict:
    out = {}
    for k, info in (links or {}).items():
        url = info.get("url")
        if not url:
            continue
        if k in ("applemusic", "itunes"):
            url = _regionalize_apple(url, for_album=False)
        out[k] = {**info, "url": url}
    return out


# ====== HTTP CLIENT ======
HTTP_CLIENT: httpx.AsyncClient | None = None
ODESLI_SEM = asyncio.Semaphore(ODESLI_MAX_CONCURRENCY)
ODESLI_LOCK = asyncio.Lock()
_LAST_ODESLI_CALL = 0.0


def get_http_client() -> httpx.AsyncClient:
    global HTTP_CLIENT
    if HTTP_CLIENT is None:
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        headers = {
            "User-Agent": "psybros-bot/2.0",
            "Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8",
        }
        HTTP_CLIENT = httpx.AsyncClient(timeout=15, limits=limits, headers=headers, follow_redirects=True)
    return HTTP_CLIENT


async def _throttle_odesli(min_gap: float = 1.2):
    global _LAST_ODESLI_CALL
    async with ODESLI_LOCK:
        now = time.time()
        delta = now - _LAST_ODESLI_CALL
        if delta < min_gap:
            await asyncio.sleep(min_gap - delta)
        _LAST_ODESLI_CALL = time.time()


# ====== ARTISTAS ======
def _apple_artist_slug_to_name(path: str) -> str | None:
    parts = [p for p in path.strip("/").split("/") if p]
    if "artist" in parts:
        try:
            i = parts.index("artist")
            if i + 1 < len(parts):
                slug = unquote(parts[i + 1])
                name = slug.replace("-", " ").strip()
                return name.title() if name else None
        except Exception:
            return None
    return None


async def _spotify_oembed(url: str) -> dict | None:
    cache_key = f"spotify_oembed::{normalize_music_url(url)}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    oembed = f"https://open.spotify.com/oembed?url={quote(normalize_music_url(url), safe='')}"
    try:
        client = get_http_client()
        r = await client.get(oembed, timeout=10)
        if r.status_code == 200:
            data = r.json() or {}
            ttl_set(GENERIC_CACHE, cache_key, data, GENERIC_CACHE_TTL)
            return data
    except Exception as e:
        log.debug(f"Spotify oEmbed falló: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 600)
    return None


async def _spotify_artist_name_from_oembed(url: str) -> str | None:
    data = await _spotify_oembed(url)
    if not data:
        return None
    title = str(data.get("title") or "").strip()
    author = str(data.get("author_name") or "").strip()
    if author:
        return author
    if title and " - " in title:
        left, right = title.split(" - ", 1)
        # en algunos casos puede venir "Artist - Song"
        return left.strip() if left.strip() else right.strip()
    return title or None


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


# ====== SPOTIFY NATIVE RESOLVER ======
SPOTIFY_CACHE: dict[str, tuple[float, object]] = {}


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
    t = re.sub(r'\s*\(with[^\)]*\)', '', t, flags=re.I)
    t = re.sub(r'\s*\[[^\]]*\]\s*$', '', t)
    return t.strip(" -")


def _clean_artist(artist: str) -> str:
    a = _normalize_unicode(artist or "")
    a = re.split(r'[,&/]|feat\.?', a, flags=re.I)[0]
    return a.strip()


def _spotify_kind_from_url(url: str) -> str:
    p = urlparse(normalize_music_url(url))
    path = p.path.lower()
    if "/track/" in path:
        return "track"
    if "/album/" in path:
        return "album"
    if "/artist/" in path:
        return "artist"
    if "/playlist/" in path:
        return "playlist"
    return "unknown"


def _split_spotify_title(title: str, author_name: str = "") -> tuple[str | None, str | None]:
    title = _normalize_unicode(title or "")
    author_name = _normalize_unicode(author_name or "")
    if not title and not author_name:
        return None, None

    # prefer author_name if provided by oEmbed
    if author_name:
        artist = author_name.strip()
        song = title.strip()
        # algunos oEmbed devuelven "song" tal cual; otros mezclan
        if " - " in song:
            left, right = song.split(" - ", 1)
            if left.strip().lower() == artist.strip().lower():
                song = right.strip()
        return artist or None, song or None

    if " - " in title:
        left, right = title.split(" - ", 1)
        return left.strip() or None, right.strip() or None
    return None, title.strip() or None


def _spotify_entity_id(url: str) -> str | None:
    p = urlparse(normalize_music_url(url))
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 2:
        return parts[-1]
    return None


async def _spotify_embed_metadata(url: str) -> dict | None:
    cache_key = f"spotify_embed_meta::{normalize_music_url(url)}"
    cached = ttl_get(SPOTIFY_CACHE, cache_key)
    if cached is not None:
        return cached

    entity_id = _spotify_entity_id(url)
    kind = _spotify_kind_from_url(url)
    data = await _spotify_oembed(url)
    if not data:
        ttl_set(SPOTIFY_CACHE, cache_key, None, 900)
        return None

    title = str(data.get("title") or "").strip()
    author_name = str(data.get("author_name") or "").strip()
    thumbnail = data.get("thumbnail_url") or data.get("thumbnailUrl")

    artist, song_or_name = _split_spotify_title(title, author_name)

    meta = {
        "kind": kind,
        "id": entity_id,
        "url": normalize_music_url(url),
        "title": title or song_or_name,
        "artist": artist,
        "name": song_or_name or title,
        "thumbnail": thumbnail,
        "author_name": author_name,
    }
    ttl_set(SPOTIFY_CACHE, cache_key, meta, SPOTIFY_CACHE_TTL)
    return meta


async def apple_search_track(artist: str, title: str) -> tuple[str | None, str | None, str | None]:
    term = " ".join(x for x in [artist, title] if x).strip()
    cache_key = f"apple_track::{COUNTRY}::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    params = {"term": term, "entity": "song", "limit": 1, "country": COUNTRY, "media": "music"}
    url = "https://itunes.apple.com/search"
    try:
        client = get_http_client()
        r = await client.get(url, params=params, timeout=12)
        if r.status_code != 200:
            ttl_set(GENERIC_CACHE, cache_key, (None, None, None), 1800)
            return None, None, None
        data = r.json() or {}
        results = data.get("results") or []
        if not results:
            ttl_set(GENERIC_CACHE, cache_key, (None, None, None), 1800)
            return None, None, None
        it = results[0]
        result = (_regionalize_apple(it.get("trackViewUrl"), for_album=False), it.get("isrc"), it.get("collectionViewUrl"))
        ttl_set(GENERIC_CACHE, cache_key, result, GENERIC_CACHE_TTL)
        return result
    except Exception as e:
        log.debug(f"Apple search falló para '{term}': {e}")
        ttl_set(GENERIC_CACHE, cache_key, (None, None, None), 1800)
        return None, None, None


async def apple_search_album(artist: str, album: str) -> str | None:
    term = " ".join(x for x in [artist, album] if x).strip()
    cache_key = f"apple_album::{COUNTRY}::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    params = {"term": term, "entity": "album", "limit": 1, "country": COUNTRY, "media": "music"}
    try:
        client = get_http_client()
        r = await client.get("https://itunes.apple.com/search", params=params, timeout=12)
        if r.status_code == 200:
            data = r.json() or {}
            results = data.get("results") or []
            if results:
                out = _regionalize_apple(results[0].get("collectionViewUrl"), for_album=True)
                ttl_set(GENERIC_CACHE, cache_key, out, GENERIC_CACHE_TTL)
                return out
    except Exception as e:
        log.debug(f"apple_search_album error: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def apple_search_artist(artist: str) -> str | None:
    term = artist.strip()
    cache_key = f"apple_artist::{COUNTRY}::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    params = {"term": term, "entity": "musicArtist", "limit": 1, "country": COUNTRY, "media": "music"}
    try:
        client = get_http_client()
        r = await client.get("https://itunes.apple.com/search", params=params, timeout=12)
        if r.status_code == 200:
            data = r.json() or {}
            results = data.get("results") or []
            if results:
                out = _regionalize_apple(results[0].get("artistLinkUrl"), for_album=False)
                ttl_set(GENERIC_CACHE, cache_key, out, GENERIC_CACHE_TTL)
                return out
    except Exception as e:
        log.debug(f"apple_search_artist error: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


DDG_HTML = "https://duckduckgo.com/html/?q={q}"


def decode_ddg_redirect(link: str) -> str:
    try:
        if not link:
            return link
        if link.startswith("/l/") or "duckduckgo.com/l/?" in link:
            full = "https://duckduckgo.com" + link if link.startswith("/l/") else link
            qs = parse_qs(urlparse(full).query)
            target = qs.get("uddg") or qs.get("u") or []
            if target:
                return unquote(target[0])
        return link
    except Exception:
        return link


async def _ddg_first_result(query: str, must_contain: str | None = None) -> str | None:
    cache_key = f"ddg_any::{query}::{must_contain or ''}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    url = DDG_HTML.format(q=quote_plus(query))
    try:
        client = get_http_client()
        r = await client.get(url, timeout=10)
        html_text = r.text or ""
        matches = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html_text)
        for raw in matches:
            link = decode_ddg_redirect(raw)
            if must_contain and must_contain not in link:
                continue
            ttl_set(GENERIC_CACHE, cache_key, link, GENERIC_CACHE_TTL)
            return link
    except Exception as e:
        log.debug(f"DDG error: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def spotify_url_from_isrc(isrc: str) -> str | None:
    if not isrc:
        return None

    cache_key = f"spotify_isrc::{isrc}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    search_url = f"https://open.spotify.com/search/{quote('isrc:' + isrc)}"
    try:
        client = get_http_client()
        r = await client.get(search_url, timeout=12)
        html_text = r.text or ""
        m = re.search(r'open\.spotify\.com/track/([A-Za-z0-9]+)', html_text)
        if m:
            result = f"https://open.spotify.com/track/{m.group(1)}"
            ttl_set(GENERIC_CACHE, cache_key, result, GENERIC_CACHE_TTL)
            return result
    except Exception as e:
        log.debug(f"spotify_url_from_isrc error: {e}")

    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def ddg_spotify_track(artist: str, title: str) -> str | None:
    q_title = _clean_title(title or "")
    q_artist = _clean_artist(artist or "")
    if not q_title:
        return None

    query = f'site:open.spotify.com/track "{q_title}" "{q_artist}"'
    return await _ddg_first_result(query, must_contain="open.spotify.com/track/")


async def spotify_search_track_scrape(artist: str, title: str) -> str | None:
    term = _clean_title(f"{artist} {title}".strip())
    if not term:
        return None

    cache_key = f"spotify_scrape::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached

    search_url = f"https://open.spotify.com/search/{quote(term)}"
    try:
        client = get_http_client()
        r = await client.get(search_url, timeout=12)
        html_text = r.text or ""
        m = re.search(r'open\.spotify\.com/track/([A-Za-z0-9]+)', html_text)
        if m:
            result = f"https://open.spotify.com/track/{m.group(1)}"
            ttl_set(GENERIC_CACHE, cache_key, result, GENERIC_CACHE_TTL)
            return result
    except Exception as e:
        log.debug(f"spotify_search_track_scrape error: {e}")

    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def build_spotify_track_links(meta: dict) -> tuple[dict, dict]:
    artist = _clean_artist(meta.get("artist") or meta.get("author_name") or "")
    title = _clean_title(meta.get("name") or meta.get("title") or "")
    if not title:
        title = _clean_title(meta.get("title") or "")
    query = quote_plus(" ".join(x for x in [artist, title] if x).strip())

    links: dict[str, dict] = {
        "spotify": {"url": meta["url"]},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search?q={query}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={query}&item_type=t"},
    }

    apple_track, isrc, apple_album = await apple_search_track(artist, title)
    if apple_track:
        links["applemusic"] = {"url": apple_track}

    # A few optional direct enrichments if available
    ddg_yt = await _ddg_first_result(f'site:youtube.com/watch "{title}" "{artist}"', must_contain="youtube.com/watch")
    if ddg_yt:
        links["youtube"] = {"url": ddg_yt}

    ddg_sc = await _ddg_first_result(f'site:soundcloud.com "{title}" "{artist}"', must_contain="soundcloud.com/")
    if ddg_sc:
        links["soundcloud"] = {"url": ddg_sc}

    extra = {
        "artist": artist,
        "title": title,
        "isrc": isrc,
        "apple_album": _regionalize_apple(apple_album, for_album=True) if apple_album else None,
    }
    return links, extra


async def build_spotify_album_links(meta: dict) -> tuple[dict, dict]:
    artist = _clean_artist(meta.get("artist") or meta.get("author_name") or "")
    album = _clean_title(meta.get("name") or meta.get("title") or "")
    query = quote_plus(" ".join(x for x in [artist, album] if x).strip())

    links: dict[str, dict] = {
        "spotify": {"url": meta["url"]},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}+album"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search/sets?q={query}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={query}&item_type=a"},
    }

    apple_album = await apple_search_album(artist, album)
    if apple_album:
        links["applemusic"] = {"url": apple_album}

    extra = {"artist": artist, "album": album}
    return links, extra


async def build_spotify_artist_links(meta: dict) -> tuple[dict, dict]:
    artist = _clean_artist(meta.get("artist") or meta.get("name") or meta.get("title") or "")
    query = quote_plus(artist)

    links: dict[str, dict] = {
        "spotify": {"url": meta["url"]},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search/people?q={query}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={query}"},
    }

    apple_artist = await apple_search_artist(artist)
    if apple_artist:
        links["applemusic"] = {"url": apple_artist}

    extra = {"artist": artist}
    return links, extra


async def build_spotify_playlist_links(meta: dict) -> tuple[dict, dict]:
    name = _clean_title(meta.get("name") or meta.get("title") or "")
    owner = _clean_artist(meta.get("author_name") or meta.get("artist") or "")
    query = quote_plus(" ".join(x for x in [owner, name] if x).strip())

    links: dict[str, dict] = {
        "spotify": {"url": meta["url"]},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}+playlist"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search?q={query}"},
        "bandcamp": {"url": f"https://bandcamp.com/search?q={query}"},
    }
    extra = {"name": name, "owner": owner}
    return links, extra


async def resolve_spotify_native(url: str) -> tuple[dict | None, str | None, str | None, str | None, str | None, list[tuple[str, str]], dict | None]:
    """
    Returns:
    links, display_title, display_artist, cover, page_url, album_buttons, lyrics_links
    """
    url = normalize_music_url(url)
    cache_key = f"spotify_native::{url}"
    cached = ttl_get(SPOTIFY_CACHE, cache_key)
    if cached is not None:
        return cached

    meta = await _spotify_embed_metadata(url)
    if not meta:
        result = (None, None, None, None, None, [], None)
        ttl_set(SPOTIFY_CACHE, cache_key, result, 900)
        return result

    kind = meta.get("kind") or "unknown"
    cover = meta.get("thumbnail")
    page_url = url
    album_buttons: list[tuple[str, str]] = []
    lyrics_links = None
    display_title = None
    display_artist = None

    if kind == "track":
        links, extra = await build_spotify_track_links(meta)
        display_title = extra.get("title") or meta.get("name")
        display_artist = extra.get("artist") or meta.get("artist")
        if extra.get("apple_album"):
            album_buttons.append(("💿🍎", extra["apple_album"]))
        if display_title:
            lyrics_links = await get_lyrics_links(display_artist or "", display_title)
    elif kind == "album":
        links, extra = await build_spotify_album_links(meta)
        display_title = extra.get("album") or meta.get("name")
        display_artist = extra.get("artist") or meta.get("artist")
        apple_album = links.get("applemusic", {}).get("url")
        if apple_album:
            album_buttons.append(("💿🍎", apple_album))
    elif kind == "artist":
        links, extra = await build_spotify_artist_links(meta)
        display_title = None
        display_artist = extra.get("artist")
    elif kind == "playlist":
        links, extra = await build_spotify_playlist_links(meta)
        display_title = extra.get("name")
        display_artist = extra.get("owner")
    else:
        query = quote_plus(meta.get("title") or url)
        links = {
            "spotify": {"url": url},
            "youtube": {"url": f"https://www.youtube.com/results?search_query={query}"},
            "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
            "soundcloud": {"url": f"https://soundcloud.com/search?q={query}"},
        }
        display_title = meta.get("title")
        display_artist = meta.get("artist")

    result = (links, display_title, display_artist, cover, page_url, album_buttons, lyrics_links)
    ttl_set(SPOTIFY_CACHE, cache_key, result, SPOTIFY_CACHE_TTL)
    return result


# ===== Álbum =====
ALBUM_LABEL = {
    "applemusic": "💿🍎",
    "spotify": "💿🎧",
    "youtubemusic": "💿🎵",
    "youtube": "💿▶️",
    "soundcloud": "💿☁️",
}


async def _album_from_spotify(url: str):
    url = normalize_music_url(url)
    p = urlparse(url)
    if "/album/" in p.path:
        return url, None
    try:
        client = get_http_client()
        r = await client.get(url, timeout=10)
        m = re.search(r"open\.spotify\.com/album/([A-Za-z0-9]+)", r.text or "")
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
        return (
            f"https://music.youtube.com/playlist?list={lid}" if prefer_music
            else f"https://www.youtube.com/playlist?list={lid}"
        ), None
    return None, None


async def _ytm_album_from_page(url: str, prefer_music: bool = True):
    try:
        client = get_http_client()
        r = await client.get(url, timeout=12)
        html_text = r.text or ""
        m = re.search(r'"playlistId":"(OLAK[^"]+)"', html_text) or re.search(r'list=(OLAK[^"&]+)', html_text)
        if m:
            pid = m.group(1)
            return (
                f"https://music.youtube.com/playlist?list={pid}" if prefer_music
                else f"https://www.youtube.com/playlist?list={pid}"
            ), None
        m = re.search(r'"browseId":"(MPREb[^"]+)"', html_text) or re.search(r'/browse/(MPREb[^"?]+)', html_text)
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
    buttons, seen = [], set()
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
            album_url, _ = await _album_from_youtube_robust(plat_url, True)
        elif key == "youtube":
            album_url, _ = await _album_from_youtube_robust(plat_url, False)
        elif key == "soundcloud":
            album_url, _ = await _album_from_soundcloud(plat_url)
        else:
            album_url = None

        if album_url and album_url not in seen:
            seen.add(album_url)
            buttons.append((ALBUM_LABEL.get(key, "💿"), album_url))
    return buttons


# ===== Memoria links =====
STORE: dict[str, dict] = {}
ORDER = deque(maxlen=300)


def remember_links(
    links: dict,
    album_buttons: list[tuple[str, str]],
    lyrics_links: dict | None = None,
    title: str | None = None,
    artist_name: str | None = None,
    cover: str | None = None,
    page_url: str | None = None,
) -> str:
    key = uuid.uuid4().hex
    STORE[key] = {
        "links": links,
        "albums": album_buttons,
        "lyrics_links": lyrics_links,
        "title": title,
        "artist_name": artist_name,
        "cover": cover,
        "page_url": page_url,
    }
    ORDER.append(key)
    while len(STORE) > ORDER.maxlen:
        old = ORDER.popleft()
        STORE.pop(old, None)
    return key


# ===== Caches =====
GENERIC_CACHE: dict[str, tuple[float, object]] = {}
ODESLI_CACHE: dict[str, tuple[float, object]] = {}
LYRICS_CACHE: dict[str, tuple[float, object]] = {}
SETLIST_CACHE: dict[str, tuple[float, dict]] = {}


# ===== Letras =====
async def _musixmatch_share_url(artist: str, title: str) -> str | None:
    if not MUSIXMATCH_KEY:
        return None
    q_artist = _clean_artist(artist or "")
    q_title = _clean_title(title or "")
    if not q_title:
        return None
    try:
        client = get_http_client()
        r = await client.get(
            "https://api.musixmatch.com/ws/1.1/track.search",
            params={
                "q_track": q_title,
                "q_artist": q_artist,
                "page_size": 1,
                "s_track_rating": "desc",
                "f_has_lyrics": 1,
                "apikey": MUSIXMATCH_KEY
            },
            timeout=10,
        )
        data = r.json()
        track_list = (data.get("message", {}).get("body", {}) or {}).get("track_list", [])
        if not track_list:
            return None
        track = track_list[0].get("track") or {}
        return track.get("track_share_url") or None
    except Exception:
        return None


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
            client = get_http_client()
            r = await client.get(base, params=params, timeout=10)
            j = r.json() or {}
            results = j.get("result") or []
            if isinstance(results, list) and results:
                link = (results[0] or {}).get("song-link")
                if link:
                    return link
        except Exception:
            pass
    return None


async def _ddg_first_result_site(site: str, artist: str, title: str) -> str | None:
    q_title = _clean_title(title or "")
    q_artist = _clean_artist(artist or "")
    if not q_title:
        return None
    query = f'site:{site} "{q_title}" "{q_artist}" lyrics'
    return await _ddg_first_result(query, must_contain=site)


async def get_lyrics_links(artist: str | None, title: str | None) -> dict | None:
    artist = artist or ""
    title = title or ""
    if not title.strip():
        return None

    cache_key = f"lyrics::{_clean_artist(artist)}::{_clean_title(title)}"
    cached = ttl_get(LYRICS_CACHE, cache_key)
    if cached is not None:
        return cached

    mm, lc = await asyncio.gather(
        _musixmatch_share_url(artist, title),
        _lyricscom_link(artist, title),
    )
    letras, az, genius = await asyncio.gather(
        _ddg_first_result_site("www.letras.com", artist, title),
        _ddg_first_result_site("www.azlyrics.com", artist, title),
        _ddg_first_result_site("genius.com", artist, title),
    )

    result = None
    if any([mm, lc, letras, az, genius]):
        result = {
            "lyricscom": lc,
            "musixmatch": mm,
            "letras": letras,
            "azlyrics": az,
            "genius": genius,
        }
    ttl_set(LYRICS_CACHE, cache_key, result, LYRICS_CACHE_TTL)
    return result


# =================== Teclados ===================
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


def build_keyboard(
    links: dict,
    show_all: bool,
    key: str,
    album_buttons: list[tuple[str, str]],
    lyrics_links: dict | None = None
) -> InlineKeyboardMarkup:
    sorted_keys = sort_keys(links)
    fav_set = set(FAVS_LOWER)
    keys_to_show = sorted_keys if show_all else [k for k in sorted_keys if k.lower() in fav_set]

    botones = []
    fila = []
    for k in keys_to_show:
        url = links[k].get("url")
        if not url:
            continue
        label = nice_name(k)
        fila.append(InlineKeyboardButton(text=label, url=url))
        if len(fila) == 3:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)

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
            for i in range(0, len(lyr_row), 3):
                botones.append(lyr_row[i:i + 3])

    if album_buttons:
        botones.append([InlineKeyboardButton("💿 Álbum", callback_data=f"noop|{key}")])
        fila = []
        for text, url in album_buttons:
            fila.append(InlineKeyboardButton(text, url=url))
            if len(fila) == 3:
                botones.append(fila)
                fila = []
        if fila:
            botones.append(fila)

    if not show_all and len(keys_to_show) < len(sorted_keys):
        botones.append([InlineKeyboardButton("Más opciones ▾", callback_data=f"more|{key}")])
    elif show_all:
        botones.append([InlineKeyboardButton("◀ Menos opciones", callback_data=f"less|{key}")])

    return InlineKeyboardMarkup(botones)


# ===== Odesli (optional, non-critical) =====
async def fetch_odesli(url: str):
    api = "https://api.song.link/v1-alpha.1/links"
    normalized_url = normalize_music_url(url)

    cached = ttl_get(ODESLI_CACHE, normalized_url)
    if cached is not None:
        log.info(f"Odesli cache HIT: {normalized_url}")
        return cached

    params = {"url": normalized_url, "userCountry": COUNTRY}
    headers = {"Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8"}

    async with ODESLI_SEM:
        client = get_http_client()

        for attempt in range(ODESLI_MAX_RETRIES):
            try:
                await _throttle_odesli()
                r = await client.get(api, params=params, headers=headers, timeout=10)

                if r.status_code == 200:
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
                    result = (links_for_track or None, title, artist, thumb, page_url)
                    ttl_set(ODESLI_CACHE, normalized_url, result, ODESLI_CACHE_TTL)
                    log.info(f"Odesli OK: {normalized_url}")
                    return result

                if r.status_code == 429:
                    wait_s = min(2 * (attempt + 1), 6)
                    log.warning(
                        f"Odesli 429 para {normalized_url}. "
                        f"Reintento {attempt + 1}/{ODESLI_MAX_RETRIES} en {wait_s}s"
                    )
                    await asyncio.sleep(wait_s)
                    continue

                log.warning(f"Odesli devolvió {r.status_code} para {normalized_url}")
                ttl_set(ODESLI_CACHE, normalized_url, (None, None, None, None, None), 300)
                return None, None, None, None, None

            except Exception as e:
                wait_s = min(1 + attempt, 3)
                log.warning(
                    f"Odesli error intento {attempt + 1}/{ODESLI_MAX_RETRIES} "
                    f"para {normalized_url}: {e}"
                )
                await asyncio.sleep(wait_s)

    ttl_set(ODESLI_CACHE, normalized_url, (None, None, None, None, None), 300)
    return None, None, None, None, None


async def resolve_non_spotify(url: str) -> tuple[dict | None, str | None, str | None, str | None, str | None, list[tuple[str, str]], dict | None]:
    links, title, artist_name, cover, page_url = await fetch_odesli(url)
    if not links:
        return None, None, None, None, None, [], None
    # Best-effort enrichment only when Odesli already succeeded
    lyrics_links = await get_lyrics_links(artist_name or "", title or "")
    album_buttons = await derive_album_buttons_all(links)
    return links, title, artist_name, cover, page_url, album_buttons, lyrics_links


# ======== SETLIST.FM ========
ID_RE = re.compile(r"([0-9a-z]{6,12})$", re.I)


def _extract_setlist_id_from_path(path: str) -> str | None:
    last = path.rstrip("/").split("/")[-1]
    core = last.split(".html")[0]
    if "-" in core:
        cand = core.split("-")[-1]
        if ID_RE.match(cand):
            return cand.lower()
    m = ID_RE.search(core)
    return m.group(1).lower() if m else None


async def _extract_setlist_id_from_html(url: str) -> str | None:
    try:
        client = get_http_client()
        r = await client.get(url, timeout=12)
        if r.status_code != 200:
            return None
        html_text = r.text or ""
        m = re.search(r'property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
        if m:
            canon = m.group(1)
            pid = _extract_setlist_id_from_path(urlparse(canon).path)
            if pid:
                return pid
        m = re.search(r'/setlist/[^"\']*?-([0-9a-z]{6,12})\.html', html_text, re.I)
        if m:
            return m.group(1).lower()
    except Exception as e:
        log.debug(f"fetch html setlist fallback error: {e}")
    return None


def _extract_setlist_id(url: str) -> str | None:
    try:
        p = urlparse(url)
        pid = _extract_setlist_id_from_path(p.path)
        return pid
    except Exception:
        return None


async def fetch_setlist_json(setlist_id: str) -> dict | None:
    if not SETLIST_FM_API_KEY:
        log.warning("Falta SETLIST_FM_API_KEY")
        return None

    cache_key = f"setlist_json::{setlist_id}"
    cached = ttl_get(SETLIST_CACHE, cache_key)
    if cached is not None:
        return cached

    url = f"https://api.setlist.fm/rest/1.0/setlist/{setlist_id}"
    headers = {
        "x-api-key": SETLIST_FM_API_KEY,
        "Accept": "application/json",
        "Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8",
        "User-Agent": "setlist-resolver-bot/1.1",
    }
    try:
        client = get_http_client()
        r = await client.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            ttl_set(SETLIST_CACHE, cache_key, data, SETLIST_CACHE_TTL)
            return data
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
    event_date = js.get("eventDate")
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
        "artist": artist,
        "venue": venue,
        "city": city,
        "country": country,
        "eventDate": event_date,
        "url": url,
    }
    return meta, songs


async def apple_search_track_url(artist: str, title: str) -> str | None:
    url, _, _ = await apple_search_track(artist, title)
    return url


async def resolve_song_links(artist: str, title: str) -> tuple[dict | None, str | None]:
    # Prefer native searches for stability; enrich with Odesli only if available.
    apple = await apple_search_track_url(artist, title)
    query = quote_plus(f"{artist} {title}".strip())
    links = {
        "youtube": {"url": f"https://www.youtube.com/results?search_query={query}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={query}"},
        "soundcloud": {"url": f"https://soundcloud.com/search?q={query}"},
    }
    if apple:
        links["applemusic"] = {"url": apple}
        o_links, *_rest = await fetch_odesli(apple)
        if o_links:
            links.update(o_links)
    return links, None


def _pick_youtube_key(links: dict) -> str | None:
    if "youtubemusic" in links:
        return "youtubemusic"
    if "youtube" in links:
        return "youtube"
    return None


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
    return f"{base} (cover de {cover})" if cover else base


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

        if "spotify" in links and links["spotify"].get("url"):
            fila.append(InlineKeyboardButton("Espotifai", url=links["spotify"]["url"]))

        yk = _pick_youtube_key(links)
        if yk and links.get(yk, {}).get("url"):
            fila.append(InlineKeyboardButton(nice_name(yk), url=links[yk]["url"]))

        if "applemusic" in links and links["applemusic"].get("url"):
            fila.append(InlineKeyboardButton("Manzanita", url=links["applemusic"]["url"]))

        if page_url:
            fila.append(InlineKeyboardButton("⋯ Más", url=page_url))
        else:
            q = quote_plus(f"{(entry['meta'] or {}).get('artist', '')} {it.get('title', '')}".strip())
            fila.append(InlineKeyboardButton("⋯ Buscar", url=f"https://song.link/search?q={q}"))

        if fila:
            botones.append(fila)

    if pages > 1:
        prev_page = max(0, page - 1)
        next_page = min(pages - 1, page + 1)
        botones.append([
            InlineKeyboardButton("◀", callback_data=f"slp|{key}|{prev_page}"),
            InlineKeyboardButton(f"Página {page + 1}/{pages}", callback_data=f"noop|{key}"),
            InlineKeyboardButton("▶", callback_data=f"slp|{key}|{next_page}"),
        ])

    return InlineKeyboardMarkup(botones)


async def handle_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    setlist_id = _extract_setlist_id(url)
    if not setlist_id:
        setlist_id = await _extract_setlist_id_from_html(url)

    if not setlist_id:
        await update.message.reply_text("No pude leer el ID del setlist en esa URL.")
        return

    if not SETLIST_FM_API_KEY:
        await update.message.reply_text("Falta SETLIST_FM_API_KEY en el entorno para usar setlist.fm.")
        return

    cached = ttl_get(SETLIST_CACHE, f"setlist_bundle::{setlist_id}")
    if not cached:
        js = await fetch_setlist_json(setlist_id)
        if not js:
            await update.message.reply_text("No pude obtener ese setlist (API). Intenta más tarde.")
            return

        meta, songs_raw = parse_setlist_songs(js)
        songs_raw = [s for s in songs_raw if not s.get("is_tape")]
        cached = {"meta": meta, "songs_raw": songs_raw}
        ttl_set(SETLIST_CACHE, f"setlist_bundle::{setlist_id}", cached, SETLIST_CACHE_TTL)

    artist_show = (cached["meta"] or {}).get("artist") or ""
    songs_raw = cached["songs_raw"]
    sem = asyncio.Semaphore(SETLIST_MAX_CONCURRENCY)
    resolved: list[dict] = []

    await update.message.reply_text("Procesando setlist…")

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


def build_caption(title: str | None, artist_name: str | None, kind_hint: str | None = None) -> str:
    if title and artist_name:
        return f"🎵 {title} — {artist_name}\n🎶 Disponible en:"
    if title:
        if kind_hint == "playlist":
            return f"📃 {title}\n🎶 Disponible en:"
        if kind_hint == "album":
            return f"💿 {title}\n🎶 Disponible en:"
        return f"🎵 {title}\n🎶 Disponible en:"
    if artist_name:
        return f"🧑‍🎤 {artist_name}\n🔎 Disponible en:"
    return "🎶 Disponible en:"


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

            if is_spotify_url(url):
                links, title, artist_name, cover, page_url, album_buttons, lyrics_links = await resolve_spotify_native(url)
                if not links:
                    await update.message.reply_text("No pude leer ese enlace de Spotify ahora. Intenta de nuevo en un momento.")
                    continue
                key = remember_links(
                    links=links,
                    album_buttons=album_buttons,
                    lyrics_links=lyrics_links,
                    title=title,
                    artist_name=artist_name,
                    cover=cover,
                    page_url=page_url,
                )
                keyboard = build_keyboard(
                    links,
                    show_all=False,
                    key=key,
                    album_buttons=album_buttons,
                    lyrics_links=lyrics_links,
                )
                kind_hint = _spotify_kind_from_url(url)
                caption = build_caption(title, artist_name, kind_hint=kind_hint)
                if cover:
                    try:
                        await context.bot.send_photo(
                            chat_id=update.effective_chat.id,
                            photo=cover,
                            caption=caption,
                            reply_markup=keyboard,
                        )
                        continue
                    except Exception as e:
                        log.info(f"No pude usar la portada Spotify, envío texto. {e}")
                await update.message.reply_text(caption, reply_markup=keyboard)
                continue

            links, title, artist_name, cover, page_url, album_buttons, lyrics_links = await resolve_non_spotify(url)
            if not links:
                await update.message.reply_text("No pude resolver ese enlace ahora. Intenta de nuevo en un momento.")
                continue

            key = remember_links(
                links=links,
                album_buttons=album_buttons,
                lyrics_links=lyrics_links,
                title=title,
                artist_name=artist_name,
                cover=cover,
                page_url=page_url,
            )
            keyboard = build_keyboard(
                links,
                show_all=False,
                key=key,
                album_buttons=album_buttons,
                lyrics_links=lyrics_links,
            )

            caption = build_caption(title, artist_name)
            if cover:
                try:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=cover,
                        caption=caption,
                        reply_markup=keyboard,
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
    if is_setlist_url(url) or (not is_music_url(url)):
        await update.inline_query.answer([], cache_time=10, is_personal=True)
        return

    artist = await detect_artist(url)
    if artist:
        name = artist["name"]
        caption = f"🧑‍🎤 {name}\n🔎 Búscalo en:"
        kb = build_artist_search_keyboard(name)
        rid = str(uuid.uuid4())
        results = [
            InlineQueryResultArticle(
                id=rid,
                title=f"Artista: {name}",
                input_message_content=InputTextMessageContent(caption),
                reply_markup=kb,
                description="Buscar al artista en otras plataformas",
            )
        ]
        await update.inline_query.answer(results, cache_time=10, is_personal=True)
        return

    if is_spotify_url(url):
        links, title, artist_name, cover, page_url, album_buttons, lyrics_links = await resolve_spotify_native(url)
    else:
        links, title, artist_name, cover, page_url, album_buttons, lyrics_links = await resolve_non_spotify(url)

    if not links:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    key = remember_links(
        links=links,
        album_buttons=album_buttons,
        lyrics_links=lyrics_links,
        title=title,
        artist_name=artist_name,
        cover=cover,
        page_url=page_url,
    )
    keyboard = build_keyboard(
        links,
        show_all=False,
        key=key,
        album_buttons=album_buttons,
        lyrics_links=lyrics_links,
    )

    kind_hint = _spotify_kind_from_url(url) if is_spotify_url(url) else None
    caption = build_caption(title, artist_name, kind_hint=kind_hint)

    rid = str(uuid.uuid4())
    if cover:
        results = [
            InlineQueryResultPhoto(
                id=rid,
                photo_url=cover,
                thumb_url=cover,
                caption=caption,
                reply_markup=keyboard,
                title=title or "Plataformas",
            )
        ]
    else:
        results = [
            InlineQueryResultArticle(
                id=rid,
                title=title or artist_name or "Plataformas",
                input_message_content=InputTextMessageContent(caption),
                reply_markup=keyboard,
                description="Enviar accesos a otras plataformas",
            )
        ]

    await update.inline_query.answer(results, cache_time=10, is_personal=True)


# -------- Callbacks --------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    data = cq.data or ""

    if data.startswith("noop|"):
        return

    if data.startswith("slp|"):
        _, key, page_s = data.split("|", 2)
        try:
            page = int(page_s)
        except Exception:
            page = 0

        keyboard = build_setlist_keyboard(key, page=page)
        try:
            if cq.inline_message_id:
                await context.bot.edit_message_reply_markup(
                    inline_message_id=cq.inline_message_id,
                    reply_markup=keyboard,
                )
            else:
                await context.bot.edit_message_reply_markup(
                    chat_id=cq.message.chat_id,
                    message_id=cq.message.message_id,
                    reply_markup=keyboard,
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
    lyrics_links = entry.get("lyrics_links")
    show_all = data.startswith("more|")

    keyboard = build_keyboard(
        links,
        show_all=show_all,
        key=key,
        album_buttons=album_buttons,
        lyrics_links=lyrics_links,
    )
    try:
        if cq.inline_message_id:
            await context.bot.edit_message_reply_markup(
                inline_message_id=cq.inline_message_id,
                reply_markup=keyboard,
            )
        else:
            await context.bot.edit_message_reply_markup(
                chat_id=cq.message.chat_id,
                message_id=cq.message.message_id,
                reply_markup=keyboard,
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


async def shutdown_http_client():
    global HTTP_CLIENT
    if HTTP_CLIENT is not None:
        try:
            await HTTP_CLIENT.aclose()
        except Exception:
            pass
        HTTP_CLIENT = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        try:
            asyncio.run(shutdown_http_client())
        except Exception:
            pass
