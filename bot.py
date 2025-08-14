<<<<<<< HEAD
# bot.py
import os
import re
import uuid
import logging
import httpx
from collections import deque
from urllib.parse import urlparse, urlunparse, parse_qs

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
log = logging.getLogger("odesli-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL").upper()  # región preferida

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

# Favoritos (orden)
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
    if parts and len(parts[0]) == 2:  # ya trae país
        parts[0] = COUNTRY.lower()
        return "/" + "/".join(parts)
    return f"/{COUNTRY.lower()}/{path.strip('/')}"

def _regionalize_apple(url: str, for_album: bool = False) -> str:
    """
    Para TRACK: normaliza host/región y conserva query (?i=).
    Para ÁLBUM: normaliza host/región y quita query.
    """
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
            url = _regionalize_apple(url, for_album=False)  # conserva ?i=
        out[k] = {**info, "url": url}
    return out

# ===== Derivar enlaces de ÁLBUM por plataforma =====
# 🔽 Etiquetas compactas con emojis para MÓVIL
ALBUM_LABEL = {
    "applemusic": "💿🍎",
    "spotify": "💿🎧",
    "youtubemusic": "💿🎵",
    "youtube": "💿▶️",
    "soundcloud": "💿☁️",
}

def _album_from_apple(url: str):
    return _regionalize_apple(url, for_album=True), None

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

async def _ytm_album_from_page(url: str, prefer_music: bool = True):
    """Scrape: busca playlistId OLAK o browseId MPREb en la página."""
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
    """Devuelve botones de álbum para TODAS las plataformas donde se puedan inferir."""
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
            buttons.append((ALBUM_LABEL[key], album_url))
    return buttons

# ===== Teclado =====
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

    # 1) CANCION (favoritos primero)
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

    # 2) ÁLBUM (sección separada, etiquetas con emojis)
    if album_buttons:
        botones.append([InlineKeyboardButton("💿 Álbum", callback_data=f"noop|{key}")])
        fila = []
        for text, url in album_buttons:
            fila.append(InlineKeyboardButton(text, url=url))
            if len(fila) == 3:
                botones.append(fila); fila = []
        if fila:
            botones.append(fila)

    # 3) Expandir/colapsar plataformas de la CANCIÓN
    if not show_all and len(keys_to_show) < len(sorted_keys):
        botones.append([InlineKeyboardButton("Más opciones ▾", callback_data=f"more|{key}")])
    elif show_all:
        botones.append([InlineKeyboardButton("◀ Menos opciones", callback_data=f"less|{key}")])

    return InlineKeyboardMarkup(botones)

# ===== Odesli =====
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
        links_norm = normalize_links(raw_links)
        links_for_track = regionalize_links_for_track(links_norm)

        uid = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
        return links_for_track or None, entity.get("title"), entity.get("artistName"), entity.get("thumbnailUrl")
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

if __name__ == "__main__":
    app = (Application.builder().token(BOT_TOKEN).post_init(_post_init).build())
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(CallbackQueryHandler(callbacks))
    log.info("✅ Bot listo. Escribe o usa @Bot en cualquier chat.")
    app.run_polling(drop_pending_updates=True)
=======
import os
import re
from typing import Dict, Tuple

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_COUNTRY = os.environ.get("ODESLI_COUNTRY", "CL")
# Render Web Service suele exponer esta URL pública:
BASE_URL = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")
PORT = int(os.environ.get("PORT", "10000"))

# Dominios soportados (el bot ignora el resto)
SUPPORTED_DOMAINS = (
    "spotify.com",
    "music.apple.com",
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "soundcloud.com",
)

# Nombres “cortos” para TRACKS
FRIENDLY_NAMES = {
    "spotify": "Espotifai",
    "youtube": "Yutú",
    "youtubemusic": "Yutúmusic",
    "applemusic": "Manzanita",
    "itunes": "Manzanita",
    "soundcloud": "SounClou",
}
# Orden de prioridad para la fila principal de TRACKS
TRACK_PRIORITY = ["spotify", "youtube", "youtubemusic", "applemusic", "soundcloud"]

# Etiquetas compactas para ÁLBUM (emojis)
ALBUM_EMOJI = {
    "spotify": "💿🟢",
    "applemusic": "💿🍎",
    "itunes": "💿🍎",
    "youtube": "💿▶️",
    "youtubemusic": "💿🎵",
    "soundcloud": "💿☁️",
}
ALBUM_PRIORITY = ["spotify", "applemusic", "youtube", "youtubemusic", "soundcloud"]


def _norm_platform(p: str) -> str:
    """Normaliza la clave de plataforma devuelta por Odesli."""
    p = (p or "").strip().lower()
    # unificar variantes comunes
    if p in ("itunes", "apple", "apple music"):
        return "applemusic"
    if p in ("youtubemusic", "youtube music", "youtube_music"):
        return "youtubemusic"
    if p == "soundcloud":
        return "soundcloud"
    if p == "spotify":
        return "spotify"
    if p == "youtube":
        return "youtube"
    return p


def _is_supported_url(text: str) -> Tuple[bool, str]:
    m = re.search(r"https?://\S+", text or "")
    if not m:
        return False, ""
    url = m.group(0)
    if not any(d in url for d in SUPPORTED_DOMAINS):
        return False, ""
    return True, url


async def _call_odesli(url: str, country: str) -> Dict:
    api = f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry={country}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(api)
        r.raise_for_status()
        return r.json()


def _split_track_and_album_links(data: Dict) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Separa links por plataforma en:
      - tracks: {platform: url}
      - albums: {platform: url}
    Basado en el tipo de entidad (song/album) de Odesli.
    """
    links_by_platform = data.get("linksByPlatform", {}) or {}
    entities = data.get("entitiesByUniqueId", {}) or {}

    track_links: Dict[str, str] = {}
    album_links: Dict[str, str] = {}

    for plat_key, info in links_by_platform.items():
        url = (info or {}).get("url")
        ent_id = (info or {}).get("entityUniqueId")
        ent = entities.get(ent_id, {}) if ent_id else {}
        ent_type = (ent.get("type") or "").lower()

        plat = _norm_platform(plat_key)
        if not url:
            continue

        if "album" in ent_type:
            album_links[plat] = url
        else:
            # por defecto tratamos como track
            track_links[plat] = url

    return track_links, album_links


def _extract_title_artist_thumb(data: Dict) -> Tuple[str, str, str]:
    entities = data.get("entitiesByUniqueId", {}) or {}
    uid = data.get("entityUniqueId")
    ent = entities.get(uid, {}) if uid else {}

    title = ent.get("title") or ""
    artist = ent.get("artistName") or ""
    thumb = ent.get("thumbnailUrl") or ent.get("imageUrl") or ""

    return title, artist, thumb


def _ordered_items(d: Dict[str, str], priority: list) -> list:
    # primero los de la prioridad, luego el resto en orden alfabético
    first = [k for k in priority if k in d]
    rest = sorted([k for k in d.keys() if k not in first])
    return first + rest


def _track_label(plat: str) -> str:
    return FRIENDLY_NAMES.get(plat, plat.capitalize())


def _album_label(plat: str) -> str:
    return ALBUM_EMOJI.get(plat, "💿")


def _build_keyboard(data: Dict) -> InlineKeyboardMarkup:
    page_url = data.get("pageUrl") or data.get("url") or None
    tracks, albums = _split_track_and_album_links(data)

    rows = []

    # ========== Fila(s) principales: TRACKS ==========
    if tracks:
        ordered = _ordered_items(tracks, TRACK_PRIORITY)
        # 3 columnas por fila
        row = []
        for plat in ordered:
            row.append(InlineKeyboardButton(_track_label(plat), url=tracks[plat]))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    # ========== ÁLBUM (si hay) ==========
    if albums:
        rows.append([InlineKeyboardButton("Álbum", callback_data="noop")])  # título seccion
        ordered_a = _ordered_items(albums, ALBUM_PRIORITY)
        row = []
        for plat in ordered_a:
            row.append(InlineKeyboardButton(_album_label(plat), url=albums[plat]))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    # ========== Más opciones (abre Song.link) ==========
    if page_url:
        rows.append([InlineKeyboardButton("Más opciones ▾", url=page_url)])

    return InlineKeyboardMarkup(rows)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    ok, url = _is_supported_url(update.message.text)
    if not ok:
        # Silencioso para links no soportados
        return

    # Resolver con Odesli
    try:
        data = await _call_odesli(url, DEFAULT_COUNTRY)
    except Exception as e:
        # Silencioso (o si prefieres, envía un texto de error)
        # await update.message.reply_text("No pude resolver ese enlace ahora mismo.")
        return

    title, artist, thumb = _extract_title_artist_thumb(data)
    caption_lines = []
    if title or artist:
        if artist:
            caption_lines.append(f"{title} — {artist}")
        else:
            caption_lines.append(title)
    caption_lines.append("Disponible en:")
    caption = "🎵 " + "\n".join(caption_lines)

    kb = _build_keyboard(data)

    try:
        if thumb:
            await update.message.reply_photo(photo=thumb, caption=caption, reply_markup=kb)
        else:
            await update.message.reply_text(text=caption, reply_markup=kb)
    except Exception:
        # fallback a mensaje simple si falla la imagen
        await update.message.reply_text(text=caption, reply_markup=kb)


def build_app() -> Application:
    return Application.builder().token(BOT_TOKEN).build()


if __name__ == "__main__":
    app = build_app()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # --- Webhook en Render Free (Web Service) ---
    if BASE_URL:
        BASE_URL = BASE_URL.rstrip("/")
        path = f"/webhook/{BOT_TOKEN}"
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=f"{BASE_URL}{path}",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
    else:
        # Para correr localmente:
        app.run_polling(drop_pending_updates=True)
>>>>>>> 965000f (setup: archivos del bot)
