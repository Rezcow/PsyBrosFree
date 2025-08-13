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

def platform_key_from_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "spotify" in host: return "spotify"
    if "music.youtube.com" in host: return "youtubemusic"
    if "youtube.com" in host or "youtu.be" in host: return "youtube"
    if "music.apple.com" in host or "geo.music.apple.com" in host or "itunes.apple.com" in host: return "applemusic"
    if "soundcloud.com" in host: return "soundcloud"
    return None

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
    """Fuerza región en enlaces de Apple Music a COUNTRY y limpia tracking."""
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
            q = parse_qs(p.query)
            new_q = {}
            if "i" in q and q["i"]:
                # Si quieres mantener ?i= para resaltar el track en Apple, comenta la siguiente línea
                new_q = {}  # <- quitamos ?i= para link del álbum
            return urlunparse((p.scheme, host, new_path, "", urlencode(new_q) if new_q else "", ""))
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

# ----- Álbum desde una URL de track -----
async def derive_album_button(original_url: str, links: dict):
    """
    Intenta obtener (btn_text, album_url, album_name_optional) desde la plataforma del link original.
    Devuelve None si no se puede inferir.
    """
    key = platform_key_from_url(original_url)
    if not key or key not in links:
        return None
    platform_url = links[key].get("url")
    if not platform_url:
        return None

    # Apple Music: quitar query (?i=...) y forzar región
    if key == "applemusic":
        album_url = _regionalize_apple(platform_url.split("?", 1)[0])
        album_name = None
        try:
            m = re.search(r"/album/([^/]+)/", urlparse(album_url).path)
            if m:
                album_name = m.group(1).replace("-", " ").strip().title()
        except Exception:
            pass
        return ("💿 Ver álbum (Manzanita)", album_url, album_name)

    # Spotify: extraer album desde el HTML del track
    if key == "spotify":
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient() as client:
                r = await client.get(platform_url, headers=headers, timeout=10)
            m = re.search(r"open\.spotify\.com/album/([A-Za-z0-9]+)", r.text)
            if m:
                album_url = f"https://open.spotify.com/album/{m.group(1)}"
                return ("💿 Ver álbum (Espotifai)", album_url, None)
        except Exception as e:
            log.debug(f"Spotify album fetch fail: {e}")

    # YouTube / YouTube Music: si hay list=OLAK... lo usamos
    if key in ("youtubemusic", "youtube"):
        p = urlparse(platform_url)
        qs = parse_qs(p.query)
        lid = qs.get("list", [None])[0]
        if lid and lid.startswith("OLAK"):
            album_url = f"https://music.youtube.com/playlist?list={lid}"
            return ("💿 Ver álbum (Yutúmusic)", album_url, None)

    # SoundCloud: parámetro ?in=user/sets/...
    if key == "soundcloud":
        p = urlparse(platform_url)
        inside = parse_qs(p.query).get("in", [None])[0]
        if inside:
            album_url = f"https://soundcloud.com/{inside}"
            return ("💿 Ver álbum (SounClou)", album_url, None)

    return None

# almacenamiento simple para callbacks
STORE: dict[str, dict] = {}
ORDER = deque(maxlen=300)
def remember_links(links: dict, album: tuple[str, str] | None) -> str:
    key = uuid.uuid4().hex
    STORE[key] = {"links": links, "album": album}
    ORDER.append(key)
    while len(STORE) > ORDER.maxlen:
        old = ORDER.popleft()
        STORE.pop(old, None)
    return key

def build_keyboard(links: dict, show_all: bool, key: str, album_button: tuple[str, str] | None) -> InlineKeyboardMarkup:
    sorted_keys = sort_keys(links)
    keys_to_show = sorted_keys if show_all else [k for k in sorted_keys if k.lower() in set(FAVS_LOWER)]

    botones = []

    # Fila de "Ver álbum" (si disponible)
    if album_button:
        btn_text, btn_url = album_button
        botones.append([InlineKeyboardButton(btn_text, url=btn_url)])

    # Plataformas
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

        # botón "Ver álbum" + posible nombre
        album_info = await derive_album_button(url, links)
        album_button = None
        album_caption = None
        if album_info:
            btn_text, album_url, album_name = album_info
            album_button = (btn_text, album_url)
            if album_name:
                album_caption = album_name

        key = remember_links(links, album_button)
        keyboard = build_keyboard(links, show_all=False, key=key, album_button=album_button)

        caption = "🎶 Disponible en:"
        if title and artist:
            caption = f"🎵 {title} — {artist}\n🎶 Disponible en:"
        elif title:
            caption = f"🎵 {title}\n🎶 Disponible en:"
        if album_caption:
            caption += f"\n💿 Álbum: {album_caption}"

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

    album_info = await derive_album_button(url, links)
    album_button = None
    album_caption = None
    if album_info:
        btn_text, album_url, album_name = album_info
        album_button = (btn_text, album_url)
        if album_name:
            album_caption = album_name

    key = remember_links(links, album_button)
    keyboard = build_keyboard(links, show_all=False, key=key, album_button=album_button)

    caption = "🎶 Disponible en:"
    if title and artist:
        caption = f"🎵 {title} — {artist}\n🎶 Disponible en:"
    elif title:
        caption = f"🎵 {title}\n🎶 Disponible en:"
    if album_caption:
        caption += f"\n💿 Álbum: {album_caption}"

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
    album_button = entry.get("album")
    show_all = data.startswith("more|")
    keyboard = build_keyboard(links, show_all=show_all, key=key, album_button=album_button)

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
