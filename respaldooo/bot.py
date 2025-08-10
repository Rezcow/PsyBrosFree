# bot.py — Bot con Rate Limit + Backoff y flujo IG híbrido (sin sesión)

import os
import re
import uuid
import asyncio
import random
import time
import mimetypes
import subprocess
import urllib.parse
import unicodedata
from pathlib import Path
from typing import Callable, Awaitable, Any, Dict, List, Tuple

import httpx
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from instaloader import Instaloader, Post
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# =========================
# Config
# =========================
BOT_VERSION = "v4.0-rl-backoff-ig-hybrid"
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPOTIPY_CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET")
DEBUG = os.environ.get("DEBUG", "0") == "1"

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ytdlp common opts
YTDLP_UA = "Mozilla/5.0"
YTDLP_RETRIES = 2

# Rate limit / backoff config
GLOBAL_CONCURRENCY = int(os.environ.get("GLOBAL_CONCURRENCY", "3"))   # ejecuciones concurrentes
PER_CHAT_MIN_INTERVAL = float(os.environ.get("PER_CHAT_MIN_INTERVAL", "1.2"))  # seg entre trabajos por chat
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "1.5"))
RETRY_FACTOR = float(os.environ.get("RETRY_FACTOR", "1.9"))
RETRY_JITTER = float(os.environ.get("RETRY_JITTER", "0.5"))

pending_youtube_links: Dict[str, str] = {}

# =========================
# Rate Limiter & Backoff
# =========================
class RateLimiter:
    def __init__(self, per_chat_min_interval: float, global_concurrency: int):
        self.per_chat_min_interval = per_chat_min_interval
        self._last_time: Dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._global_sem = asyncio.Semaphore(global_concurrency)

    async def wait(self, chat_id: int):
        # global concurrency
        await self._global_sem.acquire()
        release = True
        try:
            async with self._lock:
                now = time.monotonic()
                last = self._last_time.get(chat_id, 0.0)
                delta = now - last
                if delta < self.per_chat_min_interval:
                    await asyncio.sleep(self.per_chat_min_interval - delta)
                self._last_time[chat_id] = time.monotonic()
            release = False
        finally:
            if release:
                self._global_sem.release()

    def done(self):
        # liberar el semáforo al terminar la tarea protegida
        try:
            self._global_sem.release()
        except Exception:
            pass

rate_limiter = RateLimiter(PER_CHAT_MIN_INTERVAL, GLOBAL_CONCURRENCY)

async def with_retries(fn: Callable[..., Awaitable[Any]], *args, attempts: int = RETRY_ATTEMPTS,
                       base: float = RETRY_BASE_DELAY, factor: float = RETRY_FACTOR, jitter: float = RETRY_JITTER, **kwargs) -> Any:
    delay = base
    last_err = None
    for i in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i == attempts - 1:
                break
            await asyncio.sleep(delay + random.uniform(0, jitter))
            delay *= factor
    raise last_err

# =========================
# Utils: URL & Files
# =========================
ZERO_WIDTH = re.compile(r'[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]')
TRAILING_GARBAGE = '),.?!…>]"\'’”»'

def normalize_text(s: str) -> str:
    return ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", s or ""))

def extraer_url_limpia(texto: str) -> str | None:
    texto = normalize_text(texto)
    m = re.search(r'https?://[^\s<>]+', texto, flags=re.IGNORECASE)
    if not m:
        return None
    url = m.group(0).strip().rstrip(TRAILING_GARBAGE)
    try:
        p = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
    except Exception:
        pass
    return url

def host(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        h = (p.hostname or p.netloc or "").lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return (url or "").lower()

def detectar_plataforma(url: str) -> str:
    h = host(url)
    path = urllib.parse.urlsplit(url).path.lower()
    u = url.lower()

    if h.endswith("instagram.com") or h.endswith("instagr.am"):
        return "instagram"
    if "instagram.com/" in u or "instagr.am/" in u or "m.instagram.com/" in u:
        return "instagram"

    if "spotify.com" in h and "/track/" in path:
        return "spotify_track"
    if "spotify.com" in h and "/album/" in path:
        return "spotify_album"
    if "music.apple.com" in h and ("/song/" in path or "?i=" in u):
        return "apple_song"
    if "music.youtube.com" in h:
        return "youtube_music"
    if h.endswith("youtube.com") or h.endswith("youtu.be"):
        return "youtube"
    if "soundcloud.com" in h:
        return "soundcloud"
    if h.endswith("twitter.com") or h.endswith("x.com"):
        return "twitter"
    return "desconocido"

async def manejar_eliminacion_segura(path):
    try:
        if isinstance(path, (list, tuple, set)):
            for p in path:
                if p and os.path.exists(p): os.remove(p)
            return
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Error al eliminar {path}: {e}")

def _is_image(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    return (mime and mime.startswith("image/")) or path.suffix.lower() in {".jpg",".jpeg",".png",".webp"}

def _is_video(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    return (mime and mime.startswith("video/")) or path.suffix.lower() in {".mp4",".mov",".webm",".mkv"}

def _webp_to_jpg(p: Path) -> Path:
    if p.suffix.lower() != ".webp":
        return p
    try:
        img = Image.open(p).convert("RGB")
        out = p.with_suffix(".jpg")
        img.save(out, "JPEG", quality=95)
        p.unlink(missing_ok=True)
        return out
    except Exception as e:
        print(f"[WEBP->JPG] {e}")
        return p

def _ig_shortcode_from_url(url: str) -> str | None:
    m = re.search(r"(?:instagram\.com|instagr\.am)/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?", url)
    return m.group(1) if m else None

# =========================
# Odesli (teclado con links a otras plataformas)
# =========================
async def obtener_teclado_odesli(original_url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={original_url}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, timeout=10)
            if response.status_code != 200:
                return None
            data = response.json()
            links = data.get("linksByPlatform", {})
            botones, fila = [], []
            for nombre, info in links.items():
                url = info.get("url")
                if url:
                    fila.append(InlineKeyboardButton(text=nombre.capitalize(), url=url))
                    if len(fila) == 3:
                        botones.append(fila); fila = []
            if fila:
                botones.append(fila)
            return InlineKeyboardMarkup(botones) if botones else None
    except Exception as e:
        print(f"Odesli error: {e}")
        return None

# =========================
# Metadata general (Spotify/Apple/YT Music)
# =========================
async def obtener_metadatos_general(url: str):
    try:
        headers = {"User-Agent": YTDLP_UA}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                title = (soup.find("meta", property="og:title") or {}).get("content", "").strip()
                desc = (soup.find("meta", property="og:description") or {}).get("content", "").strip()
                artist = None
                if desc:
                    artist = desc.split("•")[0].strip()
                if title and artist:
                    return f"{title} {artist}"
                elif title:
                    return title
    except Exception as e:
        print(f"[SCRAPE] Error: {e}")

    api_url = f"https://api.song.link/v1-alpha.1/links?url={url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api_url, timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            uid = data.get("entityUniqueId")
            entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
            title = entity.get("title")
            artist = entity.get("artistName")
            if title and artist:
                return f"{title} {artist}"
            elif title:
                return title
    except Exception as e:
        print(f"Odesli error: {e}")
    return None

# =========================
# yt-dlp wrapper (resiliente)
# =========================
async def _run_ytdlp(cmd: List[str]) -> Tuple[int, str, str]:
    def _run():
        return subprocess.run(cmd, capture_output=True, text=True)
    proc = await asyncio.to_thread(_run)
    return proc.returncode, proc.stdout, proc.stderr

async def ytdlp_download(url: str, out_dir: Path, recode_to_mp4: bool = False) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--restrict-filenames",
        "-o", template,
        "--add-header", f"User-Agent: {YTDLP_UA}",
        "-R", str(YTDLP_RETRIES),
        url
    ]
    if recode_to_mp4:
        cmd += ["--recode-video", "mp4", "--merge-output-format", "mp4"]

    rc, so, se = await _run_ytdlp(cmd)
    if rc != 0:
        raise RuntimeError(se[:1000] or "yt-dlp failed")

    files = [p for p in out_dir.glob("*") if p.suffix not in {".part", ".ytdl", ".json"} and p.stat().st_size > 0]
    return files

# =========================
# Descarga por búsqueda (YouTube → MP3)
# =========================
async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    sanitized = re.sub(r'[\\/*?:"<>|]', "", query)
    output_path = os.path.join(DOWNLOADS_DIR, f"{sanitized}.mp3")
    try:
        cmd = [
            "yt-dlp",
            f"ytsearch1:{query}",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "9",
            "-o", output_path
        ]
        rc, so, se = await _run_ytdlp(cmd)
        if rc == 0 and os.path.exists(output_path):
            with open(output_path, 'rb') as audio_file:
                await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=query)
        else:
            print(so, se)
            await context.bot.send_message(chat_id=chat_id, text="❌ No se generó archivo de audio.")
    except Exception as e:
        if "Timed out" not in str(e):
            await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query} ({e})")
    finally:
        await manejar_eliminacion_segura(output_path)

# =========================
# YouTube directo (video)
# =========================
async def descargar_video_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    filename = os.path.join(DOWNLOADS_DIR, "youtube.mp4")
    descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
    try:
        cmd = ["yt-dlp", "-f", "mp4", "-o", filename, url]
        rc, so, se = await _run_ytdlp(cmd)
        if rc != 0:
            raise RuntimeError(se[:1000])
        with open(filename, 'rb') as f:
            await context.bot.send_video(chat_id=chat_id, video=f)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ YouTube error: {e}")
    finally:
        await manejar_eliminacion_segura(filename)
        try: await descargando_msg.delete()
        except: pass

# =========================
# Spotify álbum → lista de queries
# =========================
def obtener_tracks_album_spotify(album_url):
    if not (SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET):
        return [], None, None
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET
    ))
    album_id = None
    match = re.search(r"album/([a-zA-Z0-9]+)", album_url)
    if match:
        album_id = match.group(1)
    if not album_id:
        return [], None, None

    tracks = []
    cover_url = None
    album_name = None
    try:
        album = sp.album(album_id)
        album_name = album['name']
        if album.get("images"):
            cover_url = album["images"][0]["url"]
        for item in album['tracks']['items']:
            title = item['name']
            artists = [a['name'] for a in item['artists']]
            track_number = item.get('track_number', None)
            if track_number is not None:
                track_num_str = f"{track_number:02d}"
                track_query = f"{track_num_str} - {title} {', '.join(artists)}"
            else:
                track_query = f"{title} {', '.join(artists)}"
            tracks.append(track_query)
    except Exception as e:
        print(f"[Spotify Album] Error: {e}")
    return tracks, cover_url, album_name

# =========================
# Instagram (sin login): yt-dlp → instaloader → OG  (con backoff/rl)
# =========================
async def _try_ytdlp_instagram(url: str, out_dir: Path) -> List[Path]:
    async def _run():
        return await ytdlp_download(url, out_dir, recode_to_mp4=True)
    return await with_retries(_run)

async def _try_instaloader_instagram(url: str, out_dir: Path) -> List[Path]:
    sc = _ig_shortcode_from_url(url)
    if not sc:
        return []
    async def _run() -> List[Path]:
        L = Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern=""
        )
        post = Post.from_shortcode(L.context, sc)
        items: List[Tuple[str, str]] = []
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                items.append(("video" if node.is_video else "image", node.video_url if node.is_video else node.display_url))
        else:
            items.append(("video" if post.is_video else "image", post.video_url if post.is_video else post.url))

        files: List[Path] = []
        async with httpx.AsyncClient() as client:
            for typ, media_url in items:
                r = await client.get(media_url, timeout=20)
                if r.status_code == 200 and r.content:
                    ext = ".mp4" if typ == "video" else ".jpg"
                    p = out_dir / f"ig_{uuid.uuid4().hex}{ext}"
                    p.write_bytes(r.content)
                    files.append(p)
        return files
    return await with_retries(_run)

async def _og_media_from_instagram(url: str) -> List[Dict[str, str]]:
    headers = {"User-Agent": YTDLP_UA}
    async def _run() -> List[Dict[str, str]]:
        media = []
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                return media
            soup = BeautifulSoup(r.text, "html.parser")
            og_video = soup.find("meta", property="og:video")
            og_image = soup.find("meta", property="og:image")
            if og_video and og_video.get("content"):
                media.append({"type": "video", "url": og_video["content"]})
            if og_image and og_image.get("content"):
                media.append({"type": "image", "url": og_image["content"]})
        return media
    return await with_retries(_run)

# =========================
# Handler principal de mensajes (con rate limit por chat)
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    # Rate limit per chat + global concurrency
    await rate_limiter.wait(chat_id)
    try:
        url = extraer_url_limpia(text)
        if not url:
            return

        plataforma = detectar_plataforma(url)

        if DEBUG:
            await update.message.reply_text(f"[debug {BOT_VERSION}]\nhost={host(url)}\nplataforma={plataforma}\n{url}")

        procesando_msg = await update.message.reply_text("🔎 Procesando...")

        teclado = await obtener_teclado_odesli(url)
        if teclado:
            await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)

        # YOUTUBE: pregunta por Audio/Video
        if plataforma == "youtube":
            link_id = str(uuid.uuid4())
            pending_youtube_links[link_id] = url
            botones = [[
                InlineKeyboardButton("🎬 Video", callback_data=f"ytvideo|{link_id}|{chat_id}"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"ytaudio|{link_id}|{chat_id}")
            ]]
            await update.message.reply_text("¿Qué formato deseas recibir?", reply_markup=InlineKeyboardMarkup(botones))
            try: await procesando_msg.delete()
            except: pass
            return

        # TRACKS: Spotify, Apple Music, YouTube Music
        if plataforma in ["spotify_track", "apple_song", "youtube_music"]:
            query = await obtener_metadatos_general(url)
            if not query:
                await context.bot.send_message(chat_id=chat_id, text="❌ No se pudo extraer título/artista.")
                try: await procesando_msg.delete()
                except: pass
                return

            descargando_msg = await context.bot.send_message(chat_id=chat_id, text=f"Descargando: {query}")
            await buscar_y_descargar(query, chat_id, context)
            try: await procesando_msg.delete()
            except: pass
            try: await descargando_msg.delete()
            except: pass
            return

        # ÁLBUMES SPOTIFY
        elif plataforma == "spotify_album":
            album_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Descargando álbum, esto puede tardar varios minutos...")
            tracks, cover_url, album_name = obtener_tracks_album_spotify(url)
            if not tracks:
                await context.bot.send_message(chat_id=chat_id, text="❌ No pude obtener las canciones del álbum.")
                try: await procesando_msg.delete()
                except: pass
                try: await album_msg.delete()
                except: pass
                return

            # Enviar carátula
            if cover_url:
                try:
                    cover_path = os.path.join(DOWNLOADS_DIR, "cover.jpg")
                    async with httpx.AsyncClient() as client:
                        r = await client.get(cover_url)
                        with open(cover_path, "wb") as img:
                            img.write(r.content)
                    caption = f"🎵 Álbum: {album_name}" if album_name else "🎵 Álbum"
                    with open(cover_path, "rb") as img:
                        await context.bot.send_photo(chat_id=chat_id, photo=img, caption=caption)
                    await manejar_eliminacion_segura(cover_path)
                except Exception as e:
                    print(f"[COVER] Error al enviar carátula: {e}")

            try: await album_msg.delete()
            except: pass

            for idx, q in enumerate(tracks, 1):
                try:
                    descargando_msg = await context.bot.send_message(chat_id=chat_id, text=f"🎵 [{idx}/{len(tracks)}] Descargando: {q}")
                    await buscar_y_descargar(q, chat_id, context)
                    try: await descargando_msg.delete()
                    except: pass
                except Exception as e:
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ Error con la canción {q}: {e}")

            await context.bot.send_message(chat_id=chat_id, text="✅ Álbum completo enviado.")
            try: await procesando_msg.delete()
            except: pass
            return

        elif plataforma == "soundcloud":
            try:
                cmd = ["scdl", "-l", url, "-o", DOWNLOADS_DIR, "-f", "--onlymp3"]
                rc, so, se = await _run_ytdlp(cmd)
                if rc != 0:
                    raise RuntimeError(se[:1000])
                for file in os.listdir(DOWNLOADS_DIR):
                    if file.endswith(".mp3"):
                        path = os.path.join(DOWNLOADS_DIR, file)
                        with open(path, 'rb') as audio_file:
                            await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                        await manejar_eliminacion_segura(path)
            except Exception as e:
                await update.message.reply_text(f"❌ SoundCloud error: {e}")
            finally:
                try: await procesando_msg.delete()
                except: pass

        elif plataforma == "instagram":
            tmp = Path(DOWNLOADS_DIR) / f"ig_{uuid.uuid4().hex}"
            msg = await context.bot.send_message(chat_id=chat_id, text="Descargando de Instagram…")
            try:
                files: List[Path] = []
                # 1) yt-dlp (con backoff)
                try:
                    files = await _try_ytdlp_instagram(url, tmp)
                except Exception as e:
                    print(f"[IG yt-dlp] {e}")
                    files = []

                # 2) Instaloader (sin login) con backoff
                if not files:
                    try:
                        files = await _try_instaloader_instagram(url, tmp)
                    except Exception as e:
                        print(f"[IG instaloader] {e}")
                        files = []

                # 3) Fallback OG (con backoff)
                if not files:
                    try:
                        og = await _og_media_from_instagram(url)
                        for item in og:
                            try:
                                async with httpx.AsyncClient() as client:
                                    r = await client.get(item["url"], timeout=20)
                                    if r.status_code == 200 and r.content:
                                        ext = ".mp4" if item["type"] == "video" else ".jpg"
                                        p = tmp / f"ig_{uuid.uuid4().hex}{ext}"
                                        p.write_bytes(r.content)
                                        files.append(p)
                            except Exception as e:
                                print(f"[IG OG DL] {e}")
                    except Exception as e:
                        print(f"[IG OG] {e}")

                if not files:
                    await update.message.reply_text("❌ No pude descargar ese enlace sin iniciar sesión.")
                else:
                    for p in sorted(files):
                        try:
                            if _is_image(p):
                                p = _webp_to_jpg(p)
                                with p.open("rb") as fh:
                                    await context.bot.send_photo(chat_id=chat_id, photo=fh)
                            elif _is_video(p):
                                try:
                                    with p.open("rb") as fh:
                                        await context.bot.send_video(chat_id=chat_id, video=fh)
                                except Exception:
                                    with p.open("rb") as fh:
                                        await context.bot.send_document(chat_id=chat_id, document=fh)
                            else:
                                with p.open("rb") as fh:
                                    await context.bot.send_document(chat_id=chat_id, document=fh)
                        except Exception as e:
                            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ No pude enviar {p.name}: {e}")
            finally:
                try:
                    for p in tmp.glob("*"): p.unlink(missing_ok=True)
                    tmp.rmdir()
                except: pass
                try: await procesando_msg.delete()
                except: pass
                try: await msg.delete()
                except: pass

        elif plataforma == "twitter":
            filename = os.path.join(DOWNLOADS_DIR, "x.mp4")
            descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
            try:
                cmd = ["yt-dlp", "-f", "mp4", "-o", filename, url]
                rc, so, se = await _run_ytdlp(cmd)
                if rc != 0:
                    raise RuntimeError(se[:1000])
                with open(filename, 'rb') as f:
                    await context.bot.send_video(chat_id=chat_id, video=f)
            except Exception as e:
                await update.message.reply_text(f"❌ Twitter/X error: {e}")
            finally:
                await manejar_eliminacion_segura(filename)
                try: await procesando_msg.delete()
                except: pass
                try: await descargando_msg.delete()
                except: pass

        else:
            await context.bot.send_message(chat_id=chat_id, text="Enlace no soportado aún.")
            try: await procesando_msg.delete()
            except: pass

    finally:
        # liberar slot de rate limit global
        rate_limiter.done()

# =========================
# Botones (YouTube Audio/Video)
# =========================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("ytvideo|") or data.startswith("ytaudio|"):
        tipo, link_id, chat_id = data.split("|", 2)
        url = pending_youtube_links.get(link_id)
        if not url:
            await context.bot.send_message(chat_id=int(chat_id), text="❌ Enlace expirado o no encontrado.")
            return

        if tipo == "ytvideo":
            filename = os.path.join(DOWNLOADS_DIR, "youtube.mp4")
            descargando_msg = await context.bot.send_message(chat_id=int(chat_id), text="Descargando video...")
            try:
                cmd = ["yt-dlp", "-f", "mp4", "-o", filename, url]
                rc, so, se = await _run_ytdlp(cmd)
                if rc != 0:
                    raise RuntimeError(se[:1000])
                with open(filename, 'rb') as f:
                    await context.bot.send_video(chat_id=int(chat_id), video=f)
            except Exception as e:
                await context.bot.send_message(chat_id=int(chat_id), text=f"❌ YouTube error: {e}")
            finally:
                await manejar_eliminacion_segura(filename)
                try: await descargando_msg.delete()
                except: pass

        elif tipo == "ytaudio":
            query_txt = await obtener_metadatos_general(url)
            if not query_txt:
                await context.bot.send_message(chat_id=int(chat_id), text="❌ No se pudo extraer título/artista.")
                return
            descargando_msg = await context.bot.send_message(chat_id=int(chat_id), text=f"Descargando: {query_txt}")
            await buscar_y_descargar(query_txt, int(chat_id), context)
            try: await descargando_msg.delete()
            except: pass

        pending_youtube_links.pop(link_id, None)

# =========================
# Main
# =========================
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print(f"✅ Bot listo {BOT_VERSION}. DEBUG={DEBUG}")
    app.run_polling()
