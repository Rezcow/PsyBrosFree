import os
import re
import uuid
import json
import html
import logging
import asyncio
import unicodedata
import time
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

MUSIXMATCH_KEY = os.environ.get("MUSIXMATCH_KEY", "").strip()
STANDS4_UID = os.environ.get("STANDS4_UID", "").strip()
STANDS4_TOKENID = os.environ.get("STANDS4_TOKENID", "").strip()
SETLIST_FM_API_KEY = os.environ.get("SETLIST_FM_API_KEY", "").strip()
SETLIST_PAGE_SIZE = int(os.environ.get("SETLIST_PAGE_SIZE", "10"))
SETLIST_MAX_CONCURRENCY = int(os.environ.get("SETLIST_MAX_CONCURRENCY", "5"))

# Rate limit / cache settings
ODESLI_MAX_CONCURRENCY = int(os.environ.get("ODESLI_MAX_CONCURRENCY", "1"))
ODESLI_MAX_RETRIES = int(os.environ.get("ODESLI_MAX_RETRIES", "2"))
ODESLI_CACHE_TTL = int(os.environ.get("ODESLI_CACHE_TTL", "21600"))
GENERIC_CACHE_TTL = int(os.environ.get("GENERIC_CACHE_TTL", "21600"))
LYRICS_CACHE_TTL = int(os.environ.get("LYRICS_CACHE_TTL", "43200"))
SETLIST_CACHE_TTL = int(os.environ.get("SETLIST_CACHE_TTL", "86400"))
SPOTIFY_CACHE_TTL = int(os.environ.get("SPOTIFY_CACHE_TTL", "21600"))

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

# ====== HTTP CLIENT ======
HTTP_CLIENT: httpx.AsyncClient | None = None
ODESLI_SEM = asyncio.Semaphore(ODESLI_MAX_CONCURRENCY)


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


def get_http_client() -> httpx.AsyncClient:
    global HTTP_CLIENT
    if HTTP_CLIENT is None:
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        headers = {
            "User-Agent": "psybros-bot/2.0",
            "Accept-Language": f"es-{COUNTRY},es;q=0.9,en;q=0.8",
        }
        HTTP_CLIENT = httpx.AsyncClient(
            timeout=15,
            limits=limits,
            headers=headers,
            follow_redirects=True,
        )
    return HTTP_CLIENT


# ====== Utils ======
def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = html.unescape(s)
    s = s.replace("\u2014", " ").replace("\u2013", " ").replace("—", " ").replace("–", " ")
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    return " ".join(s.split()).strip()


def _clean_title(title: str) -> str:
    t = _norm_text(title)
    t = re.sub(r"\s*\(feat[^\)]*\)", "", t, flags=re.I)
    t = re.sub(r"\s*\[[^\]]*\]", "", t)
    t = re.sub(r"\s*-\s*(official|audio|video|lyrics?|visualizer|live.*|remaster(ed)?).*?$", "", t, flags=re.I)
    return " ".join(t.split()).strip(" -")


def _clean_artist(artist: str) -> str:
    a = _norm_text(artist)
    a = re.split(r"\s*(?:,|&|/| feat\.? | ft\.? | x )\s*", a, maxsplit=1, flags=re.I)[0]
    return a.strip(" -")


def _safe_eq(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def build_query(artist: str | None, song: str | None, kind: str = "track") -> str:
    artist = _clean_artist(artist or "")
    song = _clean_title(song or "")
    if kind == "artist":
        return artist or song
    if kind == "album":
        if artist and song and not _safe_eq(artist, song):
            return f"{artist} {song} album"
        return f"{song} album".strip()
    # track default
    if artist and song and not _safe_eq(artist, song):
        return f"{artist} {song} official audio"
    return f"{song} official audio".strip()


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
        "napster": "Napster", "pandora": "Pandora", "tidal": "Tidal",
        "itunes": "iTunes", "yandex": "Yandex", "boomplay": "Boomplay",
        "audius": "Audius", "audiomack": "Audiomack",
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


def _regionalize_apple(url: str, for_album: bool = False) -> str:
    try:
        p = urlparse(url)
        host = "music.apple.com"
        new_path = _ensure_region_path(p.path)
        if for_album:
            return urlunparse((p.scheme or "https", host, new_path, "", "", ""))
        return urlunparse((p.scheme or "https", host, new_path, p.params, p.query, p.fragment))
    except Exception:
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


# ===== Spotify precise resolver =====
SPOTIFY_CACHE: dict[str, tuple[float, object]] = {}
GENERIC_CACHE: dict[str, tuple[float, object]] = {}
ODESLI_CACHE: dict[str, tuple[float, object]] = {}
LYRICS_CACHE: dict[str, tuple[float, object]] = {}
SETLIST_CACHE: dict[str, tuple[float, object]] = {}
APPLE_CACHE: dict[str, tuple[float, object]] = {}


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


def _extract_spotify_entity(url: str) -> tuple[str | None, str | None]:
    try:
        p = urlparse(normalize_music_url(url))
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2 and parts[0] in {"track", "album", "artist", "playlist"}:
            return parts[0], parts[1]
    except Exception:
        pass
    return None, None


def _extract_apple_entity(url: str) -> tuple[str | None, str | None]:
    try:
        p = urlparse(normalize_music_url(url))
        parts = [x for x in p.path.split("/") if x]
        for idx, part in enumerate(parts):
            if part in {"song", "album", "artist", "playlist"}:
                entity_id = None
                qs = parse_qs(p.query)
                if part == "song":
                    entity_id = (qs.get("i") or [None])[0]
                elif idx + 2 < len(parts):
                    entity_id = parts[idx + 2]
                elif idx + 1 < len(parts):
                    entity_id = parts[idx + 1]
                return ({"song": "track"}.get(part, part), entity_id)
    except Exception:
        pass
    return None, None


async def _apple_html(url: str) -> str | None:
    cache_key = f"apple_html::{normalize_music_url(url)}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        r = await client.get(normalize_music_url(url), timeout=15)
        if r.status_code == 200:
            ttl_set(GENERIC_CACHE, cache_key, r.text, GENERIC_CACHE_TTL)
            return r.text
    except Exception as e:
        log.debug(f"apple html fail: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 900)
    return None


def _parse_apple_title(raw: str) -> tuple[str | None, str | None, str | None]:
    raw = _norm_text(raw)
    raw = re.sub(r"\s*on Apple Music$", "", raw, flags=re.I)

    m = re.match(r"^(.*?)\s+by\s+(.*?)$", raw, re.I)
    if m:
        left = _clean_title(m.group(1))
        artist = _clean_artist(m.group(2))
        return None, artist or None, left or None

    m = re.match(r"^(.*?)\s*[—-]\s*(.*?)$", raw)
    if m:
        left = _clean_title(m.group(1))
        right = _clean_artist(m.group(2))
        if left and right and not _safe_eq(left, right):
            return None, right or None, left or None

    return None, None, raw or None


async def _apple_best_metadata(url: str) -> dict:
    normalized = normalize_music_url(url)
    cached = ttl_get(APPLE_CACHE, normalized)
    if cached is not None:
        return cached

    entity_type, entity_id = _extract_apple_entity(normalized)
    data = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": None,
        "artist": None,
        "album": None,
        "cover": None,
        "apple_url": normalized,
    }

    html_text = await _apple_html(normalized)
    if html_text:
        title_tag = _extract_title_tag(html_text)
        og_title = _extract_meta_content(html_text, "og:title")
        og_desc = _extract_meta_content(html_text, "og:description")
        og_image = _extract_meta_content(html_text, "og:image")
        if og_image:
            data["cover"] = og_image

        for candidate in [title_tag, og_title]:
            if candidate:
                _, artist_guess, title_guess = _parse_apple_title(candidate)
                if title_guess and not data["title"]:
                    data["title"] = title_guess
                if artist_guess and not data["artist"] and not _safe_eq(artist_guess, title_guess):
                    data["artist"] = artist_guess

        if og_desc and not data.get("artist"):
            m = re.search(r"(?:by|de)\s+(.+)$", og_desc, re.I)
            if m:
                artist_guess = _clean_artist(m.group(1))
                if artist_guess and not _safe_eq(artist_guess, data.get("title")):
                    data["artist"] = artist_guess

        records = _extract_jsonld(html_text)
        rec = _jsonld_music_record(records)
        if rec:
            rec_type = ((rec.get("@type") if not isinstance(rec.get("@type"), list) else (rec.get("@type") or [None])[0]) or "").lower()
            if not data.get("entity_type"):
                if "recording" in rec_type:
                    data["entity_type"] = "track"
                elif "album" in rec_type:
                    data["entity_type"] = "album"
                elif "playlist" in rec_type:
                    data["entity_type"] = "playlist"
                elif "group" in rec_type or "person" in rec_type:
                    data["entity_type"] = "artist"

            name = _clean_title(rec.get("name") or "")
            if name and not data["title"]:
                data["title"] = name

            by_artist = rec.get("byArtist") or rec.get("author") or rec.get("creator")
            if isinstance(by_artist, list) and by_artist:
                by_artist = by_artist[0]
            if isinstance(by_artist, dict):
                artist_name = _clean_artist(by_artist.get("name") or "")
                if artist_name and not _safe_eq(artist_name, data.get("title")):
                    data["artist"] = data["artist"] or artist_name
            elif isinstance(by_artist, str):
                artist_name = _clean_artist(by_artist)
                if artist_name and not _safe_eq(artist_name, data.get("title")):
                    data["artist"] = data["artist"] or artist_name

            image = rec.get("image")
            if isinstance(image, list) and image:
                image = image[0]
            if isinstance(image, str) and image:
                data["cover"] = data["cover"] or image

            in_album = rec.get("inAlbum")
            if isinstance(in_album, dict):
                alb_name = _clean_title(in_album.get("name") or "")
                if alb_name:
                    data["album"] = alb_name

    if data.get("artist") and _safe_eq(data.get("artist"), data.get("title")):
        data["artist"] = None

    if not data.get("entity_type"):
        data["entity_type"] = entity_type or "track"

    ttl_set(APPLE_CACHE, normalized, data, SPOTIFY_CACHE_TTL)
    return data


async def spotify_search_track(artist: str, title: str) -> str | None:
    q_title = _clean_title(title or "")
    q_artist = _clean_artist(artist or "")
    if not q_title:
        return None
    direct = await _ddg_first_result(
        f'site:open.spotify.com/track "{q_title}" "{q_artist}"',
        ("open.spotify.com",),
    )
    if direct and "/track/" in direct:
        return normalize_music_url(direct)
    q = quote(f'track:{q_title} artist:{q_artist}'.strip())
    return f"https://open.spotify.com/search/{q}"


async def spotify_search_album(artist: str, album: str) -> str | None:
    q_album = _clean_title(album or "")
    q_artist = _clean_artist(artist or "")
    if not q_album:
        return None
    direct = await _ddg_first_result(
        f'site:open.spotify.com/album "{q_album}" "{q_artist}"',
        ("open.spotify.com",),
    )
    if direct and "/album/" in direct:
        return normalize_music_url(direct)
    q = quote(f'album:{q_album} artist:{q_artist}'.strip())
    return f"https://open.spotify.com/search/{q}"


async def spotify_search_artist(artist: str) -> str | None:
    q_artist = _clean_artist(artist or "")
    if not q_artist:
        return None
    direct = await _ddg_first_result(
        f'site:open.spotify.com/artist "{q_artist}"',
        ("open.spotify.com",),
    )
    if direct and "/artist/" in direct:
        return normalize_music_url(direct)
    return f"https://open.spotify.com/search/{quote(q_artist)}"


async def complete_links_with_fallbacks(links: dict | None, entity_type: str | None, title: str | None, artist: str | None, album: str | None = None) -> dict:
    links = normalize_links(links or {})
    entity_type = (entity_type or "track").lower()
    title = _clean_title(title or "") or None
    artist = _clean_artist(artist or "") or None
    album = _clean_title(album or "") or None

    if entity_type == "artist":
        if artist or title:
            name = artist or title
            if "spotify" not in links:
                sp = await spotify_search_artist(name)
                if sp:
                    links["spotify"] = {"url": sp}
            if "youtube" not in links:
                links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(name)}"}
            if "youtubemusic" not in links:
                links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(name)}"}
            if "applemusic" not in links:
                am = await apple_search_artist(name)
                if am:
                    links["applemusic"] = {"url": _regionalize_apple(am)}
        return links

    if entity_type == "album":
        album_name = title or album
        if album_name:
            if "spotify" not in links:
                sp = await spotify_search_album(artist or "", album_name)
                if sp:
                    links["spotify"] = {"url": sp}
            q = build_query(artist, album_name, kind="album")
            if q and "youtube" not in links:
                links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(q)}"}
            if q and "youtubemusic" not in links:
                links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q)}"}
            if "applemusic" not in links:
                am = await apple_search_album(artist or "", album_name)
                if am:
                    links["applemusic"] = {"url": _regionalize_apple(am, for_album=True)}
        return links

    if entity_type == "playlist":
        q = _clean_title(title or "playlist")
        if q and "spotify" not in links:
            links["spotify"] = {"url": f"https://open.spotify.com/search/{quote(q + ' playlist')}"}
        if q and "youtube" not in links:
            links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(q + ' playlist')}"}
        if q and "youtubemusic" not in links:
            links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q + ' playlist')}"}
        if q and "applemusic" not in links:
            links["applemusic"] = {"url": f"https://music.apple.com/{COUNTRY.lower()}/search?term={quote_plus(q + ' playlist')}"}
        return links

    q = build_query(artist, title, kind="track")
    if title:
        if "spotify" not in links:
            sp = await spotify_search_track(artist or "", title)
            if sp:
                links["spotify"] = {"url": sp}
        if "youtube" not in links:
            direct_yt = await _ddg_first_result(
                f'site:youtube.com/watch "{_clean_title(title or "")}" "{_clean_artist(artist or "")}"',
                ("youtube.com",),
            )
            links["youtube"] = {"url": direct_yt or f"https://www.youtube.com/results?search_query={quote_plus(q)}"}
        if "youtubemusic" not in links and q:
            links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q)}"}
        if "applemusic" not in links:
            am, _ = await apple_search_track(artist or "", title)
            if am:
                links["applemusic"] = {"url": _regionalize_apple(am)}
        if "soundcloud" not in links and q:
            direct_sc = await _ddg_first_result(
                f'site:soundcloud.com/ "{_clean_title(title or "")}" "{_clean_artist(artist or "")}"',
                ("soundcloud.com",),
            )
            links["soundcloud"] = {"url": direct_sc or f"https://soundcloud.com/search?q={quote_plus(q)}"}

    return links


async def resolve_generic_music_url(url: str) -> tuple[dict | None, str | None, str | None, str | None, str | None]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "spotify.com" in host:
        links, title, artist_name, cover, page_url = await resolve_spotify_links(url)
        meta = {"entity_type": "track", "album": None}
    else:
        links, title, artist_name, cover, page_url = await fetch_odesli(url)
        meta = {"entity_type": None, "album": None}

        if any(x in host for x in ["music.apple.com", "itunes.apple.com", "geo.music.apple.com"]):
            ameta = await _apple_best_metadata(url)
            meta = ameta
            title = title or ameta.get("title")
            artist_name = artist_name or ameta.get("artist")
            cover = cover or ameta.get("cover")
            page_url = page_url or ameta.get("apple_url")

        links = await complete_links_with_fallbacks(
            links=links,
            entity_type=meta.get("entity_type") or "track",
            title=title,
            artist=artist_name,
            album=meta.get("album"),
        )

    return links, title, artist_name, cover, page_url


def _parse_spotify_title(raw: str) -> tuple[str | None, str | None, str | None]:
    raw = _norm_text(raw)
    raw = re.sub(r"\s*\|\s*Spotify$", "", raw, flags=re.I)

    # Track: "Origen - song and lyrics by Mecal | Spotify"
    m = re.match(r"^(.*?)\s*-\s*song and lyrics by\s*(.*?)$", raw, re.I)
    if m:
        song = _clean_title(m.group(1))
        artist = _clean_artist(m.group(2))
        return "track", artist or None, song or None

    # Album: "Album Name - Album by Artist"
    m = re.match(r"^(.*?)\s*-\s*album by\s*(.*?)$", raw, re.I)
    if m:
        album = _clean_title(m.group(1))
        artist = _clean_artist(m.group(2))
        return "album", artist or None, album or None

    # Playlist: "Playlist Name"
    # Artist page/title: "Artist Name"
    return None, None, raw or None


async def _spotify_html(url: str) -> str | None:
    cache_key = f"spotify_html::{normalize_music_url(url)}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        r = await client.get(normalize_music_url(url), timeout=15)
        if r.status_code == 200:
            ttl_set(GENERIC_CACHE, cache_key, r.text, GENERIC_CACHE_TTL)
            return r.text
    except Exception as e:
        log.debug(f"spotify html fail: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 900)
    return None


async def _spotify_oembed(url: str) -> dict | None:
    cache_key = f"spotify_oembed::{normalize_music_url(url)}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        oembed = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
        r = await client.get(oembed, timeout=10)
        if r.status_code == 200:
            data = r.json() or {}
            ttl_set(GENERIC_CACHE, cache_key, data, GENERIC_CACHE_TTL)
            return data
    except Exception as e:
        log.debug(f"spotify oembed fail: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 900)
    return None


def _extract_title_tag(html_text: str) -> str | None:
    m = re.search(r"<title>(.*?)</title>", html_text or "", re.I | re.S)
    return _norm_text(m.group(1)) if m else None


def _extract_meta_content(html_text: str, prop: str) -> str | None:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for p in patterns:
        m = re.search(p, html_text or "", re.I)
        if m:
            return _norm_text(m.group(1))
    return None


def _extract_jsonld(html_text: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text or "", re.I | re.S):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                out.extend([x for x in data if isinstance(x, dict)])
            elif isinstance(data, dict):
                out.append(data)
        except Exception:
            continue
    return out


def _jsonld_music_record(records: list[dict], want_type: str | None = None) -> dict | None:
    for rec in records:
        t = rec.get("@type")
        if isinstance(t, list):
            t = next((x for x in t if isinstance(x, str)), None)
        t = (t or "").lower()
        if want_type and want_type.lower() not in t:
            continue
        if t in {"musicrecording", "musicalbum", "musicgroup", "musicplaylist"} or not want_type:
            return rec
    return None


async def _spotify_best_metadata(url: str) -> dict:
    normalized = normalize_music_url(url)
    cached = ttl_get(SPOTIFY_CACHE, normalized)
    if cached is not None:
        return cached

    entity_type, entity_id = _extract_spotify_entity(normalized)
    data = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": None,
        "artist": None,
        "album": None,
        "cover": None,
        "spotify_url": normalized,
    }

    html_text = await _spotify_html(normalized)
    if html_text:
        title_tag = _extract_title_tag(html_text)
        og_title = _extract_meta_content(html_text, "og:title")
        og_desc = _extract_meta_content(html_text, "og:description")
        og_image = _extract_meta_content(html_text, "og:image")
        if og_image:
            data["cover"] = og_image

        for candidate in [title_tag, og_title]:
            if candidate:
                guessed_type, artist, title = _parse_spotify_title(candidate)
                if guessed_type and not data["entity_type"]:
                    data["entity_type"] = guessed_type
                if title and not data["title"]:
                    data["title"] = title
                if artist and not data["artist"] and not _safe_eq(artist, title):
                    data["artist"] = artist

        # Description often contains artist for tracks/albums
        if og_desc and not data["artist"]:
            # track description example variants are inconsistent, keep heuristic small
            m = re.search(r"(?:song|track|single|album)\s+by\s+(.+)$", og_desc, re.I)
            if m:
                artist_guess = _clean_artist(m.group(1))
                if artist_guess and not _safe_eq(artist_guess, data.get("title")):
                    data["artist"] = artist_guess

        records = _extract_jsonld(html_text)
        want_map = {
            "track": "MusicRecording",
            "album": "MusicAlbum",
            "artist": "MusicGroup",
            "playlist": "MusicPlaylist",
        }
        rec = _jsonld_music_record(records, want_map.get(data.get("entity_type"))) or _jsonld_music_record(records)
        if rec:
            name = _clean_title(rec.get("name") or "")
            if name and not data["title"]:
                data["title"] = name
            by_artist = rec.get("byArtist") or rec.get("author") or rec.get("creator")
            if isinstance(by_artist, list) and by_artist:
                by_artist = by_artist[0]
            if isinstance(by_artist, dict):
                artist_name = _clean_artist(by_artist.get("name") or "")
                if artist_name and not _safe_eq(artist_name, data.get("title")):
                    data["artist"] = data["artist"] or artist_name
            image = rec.get("image")
            if isinstance(image, list) and image:
                image = image[0]
            if isinstance(image, str) and image:
                data["cover"] = data["cover"] or image
            in_album = rec.get("inAlbum")
            if isinstance(in_album, dict):
                alb_name = _clean_title(in_album.get("name") or "")
                if alb_name:
                    data["album"] = alb_name

    oembed = await _spotify_oembed(normalized)
    if oembed:
        if not data.get("cover"):
            data["cover"] = oembed.get("thumbnail_url") or oembed.get("thumbnailUrl")
        title = _norm_text(oembed.get("title") or "")
        author = _clean_artist(oembed.get("author_name") or "")
        if title and not data.get("title"):
            guessed_type, artist_guess, title_guess = _parse_spotify_title(title)
            if title_guess:
                data["title"] = title_guess
            if artist_guess and not _safe_eq(artist_guess, title_guess):
                data["artist"] = data.get("artist") or artist_guess
        if author and not _safe_eq(author, data.get("title")):
            data["artist"] = data.get("artist") or author

    # Sanitization
    if data.get("artist") and _safe_eq(data.get("artist"), data.get("title")):
        data["artist"] = None
    if not data.get("entity_type"):
        data["entity_type"] = entity_type or "track"

    ttl_set(SPOTIFY_CACHE, normalized, data, SPOTIFY_CACHE_TTL)
    return data


async def _ddg_first_result(query: str, allow_hosts: tuple[str, ...]) -> str | None:
    cache_key = f"ddgq::{query}::{','.join(allow_hosts)}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    url = DDG_HTML.format(q=quote_plus(query))
    try:
        client = get_http_client()
        r = await client.get(url, timeout=10)
        html_text = r.text or ""
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html_text):
            link = decode_ddg_redirect(m.group(1))
            host = urlparse(link).netloc.lower()
            if any(h in host for h in allow_hosts):
                ttl_set(GENERIC_CACHE, cache_key, link, GENERIC_CACHE_TTL)
                return link
    except Exception as e:
        log.debug(f"ddg first result fail {query}: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def apple_search_track(artist: str, title: str) -> tuple[str | None, str | None]:
    term = f"{artist} {title}".strip()
    cache_key = f"apple_search_track::{COUNTRY}::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        r = await client.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": 5, "country": COUNTRY, "media": "music"},
            timeout=12,
        )
        if r.status_code == 200:
            results = (r.json() or {}).get("results") or []
            for it in results:
                track = _clean_title(it.get("trackName") or "")
                art = _clean_artist(it.get("artistName") or "")
                if title and track and title.lower() not in track.lower() and track.lower() not in title.lower():
                    continue
                if artist and art and artist.lower() not in art.lower() and art.lower() not in artist.lower():
                    continue
                out = (it.get("trackViewUrl"), it.get("isrc"))
                ttl_set(GENERIC_CACHE, cache_key, out, GENERIC_CACHE_TTL)
                return out
    except Exception as e:
        log.debug(f"apple_search_track fail: {e}")
    out = (None, None)
    ttl_set(GENERIC_CACHE, cache_key, out, 1800)
    return out


async def apple_search_album(artist: str, album: str) -> str | None:
    term = f"{artist} {album}".strip()
    cache_key = f"apple_search_album::{COUNTRY}::{term.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        r = await client.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "album", "limit": 5, "country": COUNTRY, "media": "music"},
            timeout=12,
        )
        if r.status_code == 200:
            results = (r.json() or {}).get("results") or []
            for it in results:
                alb = _clean_title(it.get("collectionName") or "")
                art = _clean_artist(it.get("artistName") or "")
                if album and alb and album.lower() not in alb.lower() and alb.lower() not in album.lower():
                    continue
                if artist and art and artist.lower() not in art.lower() and art.lower() not in artist.lower():
                    continue
                out = it.get("collectionViewUrl")
                ttl_set(GENERIC_CACHE, cache_key, out, GENERIC_CACHE_TTL)
                return out
    except Exception as e:
        log.debug(f"apple_search_album fail: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def apple_search_artist(artist: str) -> str | None:
    cache_key = f"apple_search_artist::{COUNTRY}::{artist.lower()}"
    cached = ttl_get(GENERIC_CACHE, cache_key)
    if cached is not None:
        return cached
    try:
        client = get_http_client()
        r = await client.get(
            "https://itunes.apple.com/search",
            params={"term": artist, "entity": "musicArtist", "limit": 1, "country": COUNTRY, "media": "music"},
            timeout=12,
        )
        if r.status_code == 200:
            results = (r.json() or {}).get("results") or []
            if results:
                out = results[0].get("artistLinkUrl") or results[0].get("artistViewUrl")
                ttl_set(GENERIC_CACHE, cache_key, out, GENERIC_CACHE_TTL)
                return out
    except Exception as e:
        log.debug(f"apple_search_artist fail: {e}")
    ttl_set(GENERIC_CACHE, cache_key, None, 1800)
    return None


async def resolve_spotify_links(url: str) -> tuple[dict | None, str | None, str | None, str | None, str | None]:
    meta = await _spotify_best_metadata(url)
    entity_type = meta.get("entity_type") or "track"
    title = meta.get("title")
    artist = meta.get("artist")
    album = meta.get("album")
    cover = meta.get("cover")
    spotify_url = meta.get("spotify_url") or normalize_music_url(url)

    page_url = spotify_url
    links: dict[str, dict] = {"spotify": {"url": spotify_url}}

    if entity_type == "artist":
        q = build_query(artist or title, None, kind="artist")
        if q:
            yt = f"https://www.youtube.com/results?search_query={quote_plus(q)}"
            ytm = f"https://music.youtube.com/search?q={quote_plus(q)}"
            sc = f"https://soundcloud.com/search/people?q={quote_plus(q)}"
            bc = f"https://bandcamp.com/search?q={quote_plus(q)}&item_type=b"
            am = await apple_search_artist(q)
            links.update({
                "youtube": {"url": yt},
                "youtubemusic": {"url": ytm},
                "soundcloud": {"url": sc},
                "bandcamp": {"url": bc},
            })
            if am:
                links["applemusic"] = {"url": _regionalize_apple(am)}
        return links, (artist or title), None, cover, page_url

    if entity_type == "album":
        q = build_query(artist, title, kind="album")
        if q:
            links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(q)}"}
            links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q)}"}
            links["soundcloud"] = {"url": f"https://soundcloud.com/search/albums?q={quote_plus(q)}"}
            links["bandcamp"] = {"url": f"https://bandcamp.com/search?q={quote_plus(q)}&item_type=a"}
        am = await apple_search_album(artist or "", title or album or "")
        if am:
            links["applemusic"] = {"url": _regionalize_apple(am, for_album=True)}
        return links, (title or album), artist, cover, page_url

    if entity_type == "playlist":
        q = _clean_title(title or "playlist")
        if q:
            links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(q + ' playlist')}"}
            links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q + ' playlist')}"}
            links["applemusic"] = {"url": f"https://music.apple.com/{COUNTRY.lower()}/search?term={quote_plus(q + ' playlist')}"}
            links["soundcloud"] = {"url": f"https://soundcloud.com/search/playlists?q={quote_plus(q)}"}
        return links, title, None, cover, page_url

    # track default / precise best-effort
    q = build_query(artist, title, kind="track")
    apple_url, _ = await apple_search_track(artist or "", title or "")
    if apple_url:
        links["applemusic"] = {"url": _regionalize_apple(apple_url)}

    direct_yt = None
    if q:
        direct_yt = await _ddg_first_result(
            f'site:youtube.com/watch "{_clean_title(title or "")}" "{_clean_artist(artist or "")}"',
            ("youtube.com",),
        )
        if direct_yt:
            links["youtube"] = {"url": direct_yt}
        else:
            links["youtube"] = {"url": f"https://www.youtube.com/results?search_query={quote_plus(q)}"}

        # YT Music usually lacks good public HTML results; use focused search URL
        links["youtubemusic"] = {"url": f"https://music.youtube.com/search?q={quote_plus(q)}"}

        direct_sc = await _ddg_first_result(
            f'site:soundcloud.com/ "{_clean_title(title or "")}" "{_clean_artist(artist or "")}"',
            ("soundcloud.com",),
        )
        if direct_sc:
            links["soundcloud"] = {"url": direct_sc}
        else:
            links["soundcloud"] = {"url": f"https://soundcloud.com/search?q={quote_plus(q)}"}

        direct_bc = await _ddg_first_result(
            f'site:bandcamp.com "{_clean_title(title or "")}" "{_clean_artist(artist or "")}"',
            ("bandcamp.com",),
        )
        if direct_bc:
            links["bandcamp"] = {"url": direct_bc}

    return links, title, artist, cover, page_url


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


async def _spotify_artist_name_from_oembed(url: str) -> str | None:
    meta = await _spotify_best_metadata(url)
    if meta.get("entity_type") == "artist":
        return meta.get("artist") or meta.get("title")
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
    meta = await _spotify_best_metadata(url)
    if meta.get("album") and meta.get("artist"):
        am = await apple_search_album(meta["artist"], meta["album"])
        if am:
            return am, None
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
    return await _ddg_first_result(query, (site.replace("www.", ""),))


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


# ===== Odesli (optional for non-Spotify) =====
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
                r = await client.get(api, params=params, headers=headers, timeout=12)

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
                wait_s = min(1 + attempt, 4)
                log.warning(
                    f"Odesli error intento {attempt + 1}/{ODESLI_MAX_RETRIES} "
                    f"para {normalized_url}: {e}"
                )
                await asyncio.sleep(wait_s)

    ttl_set(ODESLI_CACHE, normalized_url, (None, None, None, None, None), 300)
    return None, None, None, None, None


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


async def resolve_song_links(artist: str, title: str) -> tuple[dict | None, str | None]:
    apple_url, _ = await apple_search_track(artist, title)
    if not apple_url:
        return None, None
    links = {
        "applemusic": {"url": _regionalize_apple(apple_url)},
        "youtube": {"url": f"https://www.youtube.com/results?search_query={quote_plus(build_query(artist, title))}"},
        "youtubemusic": {"url": f"https://music.youtube.com/search?q={quote_plus(build_query(artist, title))}"},
    }
    return links, apple_url


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

            links, title, artist_name, cover, page_url = await resolve_generic_music_url(url)

            if not links:
                await update.message.reply_text("No pude resolver ese enlace ahora. Intenta de nuevo en un momento.")
                continue

            lyrics_links = await get_lyrics_links(artist_name or "", title or "")
            album_buttons = await derive_album_buttons_all(links)
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

    links, title, artist_name, cover, page_url = await resolve_generic_music_url(url)

    if not links:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    lyrics_links = await get_lyrics_links(artist_name or "", title or "")
    album_buttons = await derive_album_buttons_all(links)
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

    caption = "🎶 Disponible en:"
    if title and artist_name:
        caption = f"🎵 {title} — {artist_name}\n🎶 Disponible en:"
    elif title:
        caption = f"🎵 {title}\n🎶 Disponible en:"

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
                title=title or "Plataformas",
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
