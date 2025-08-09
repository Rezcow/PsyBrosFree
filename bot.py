import os
import re
import glob
import subprocess
import uuid
import httpx
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# =========================
# Configuración y globals
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPOTIPY_CLIENT_ID = os.environ["SPOTIPY_CLIENT_ID"]
SPOTIPY_CLIENT_SECRET = os.environ["SPOTIPY_CLIENT_SECRET"]
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Diccionario para asociar UUID -> URL real de YouTube
pending_youtube_links = {}


# =========================
# Utilidades
# =========================
def limpiar_url_params(url: str) -> str:
    return url.split("?")[0]


async def manejar_eliminacion_segura(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Error al eliminar {path}: {e}")


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
            for i, (nombre, info) in enumerate(links.items()):
                url = info.get("url")
                if url:
                    fila.append(InlineKeyboardButton(text=nombre.capitalize(), url=url))
                    if len(fila) == 3:
                        botones.append(fila)
                        fila = []
            if fila:
                botones.append(fila)
            return InlineKeyboardMarkup(botones)
    except Exception as e:
        print(f"Odesli error: {e}")
        return None


async def obtener_metadatos_general(url: str):
    # 1) Intento por OpenGraph
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
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

    # 2) Fallback por Odesli
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


def obtener_metadatos_spotify_track(track_url: str):
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET
    ))
    match = re.search(r"track/([a-zA-Z0-9]+)", track_url)
    if not match:
        return None
    track_id = match.group(1)
    try:
        track = sp.track(track_id)
        title = track["name"]
        artists = ", ".join([a["name"] for a in track["artists"]])
        return f"{title} {artists}"
    except Exception as e:
        print(f"[Spotify Track] Error: {e}")
        return None


def detectar_plataforma(url: str):
    if "spotify.com" in url and "/track/" in url:
        return "spotify_track"
    if "spotify.com" in url and "/album/" in url:
        return "spotify_album"
    if "music.apple.com" in url and ("/song/" in url or "?i=" in url):
        return "apple_song"
    if "music.youtube.com" in url:
        return "youtube_music"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "soundcloud.com" in url:
        return "soundcloud"
    if "twitter.com" in url or "x.com" in url:
        return "twitter"
    return "desconocido"


def obtener_tracks_album_spotify(album_url: str):
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
        album_name = album["name"]
        if album.get("images"):
            cover_url = album["images"][0]["url"]
        for item in album["tracks"]["items"]:
            title = item["name"]
            artists = [a["name"] for a in item["artists"]]
            track_number = item.get("track_number", None)
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
# Descargas con yt-dlp
# =========================
async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """
    Búsqueda en YouTube por título+artista y extracción directa a MP3.
    Requiere ffmpeg instalado en el sistema.
    """
    template = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    try:
        proc = subprocess.run([
            "yt-dlp",
            f"ytsearch1:{query}",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-playlist",
            "--restrict-filenames",
            "--force-ipv4",
            "-o", template
        ], capture_output=True, text=True)

        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            await context.bot.send_message(chat_id=chat_id, text="❌ Error al convertir a MP3 (revisa ffmpeg/yt-dlp).")
            return

        mp3s = sorted(glob.glob(os.path.join(DOWNLOADS_DIR, "*.mp3")), key=os.path.getmtime, reverse=True)
        if not mp3s:
            print(proc.stdout)
            print(proc.stderr)
            await context.bot.send_message(chat_id=chat_id, text="❌ No se generó archivo de audio.")
            return

        final_file = mp3s[0]
        with open(final_file, 'rb') as audio_file:
            await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=query)
        await manejar_eliminacion_segura(final_file)

    except Exception as e:
        if "Timed out" not in str(e):
            await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query} ({e})")


async def descargar_audio_desde_url(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """
    Extrae audio directo desde la URL (sin buscar).
    Intenta MP3; si falla la conversión, muestra logs y envía M4A/WebM como fallback.
    """
    template = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    try:
        proc = subprocess.run([
            "yt-dlp",
            url,
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-playlist",
            "--restrict-filenames",
            "--force-ipv4",
            "-o", template,
            "-v"
        ], capture_output=True, text=True)

        if proc.returncode != 0:
            # Fallback: descargar bestaudio sin convertir
            print("=== yt-dlp STDERR (mp3) ===")
            print(proc.stderr)
            await context.bot.send_message(chat_id=chat_id, text="⚠️ MP3 falló, intento fallback con audio original…")

            proc2 = subprocess.run([
                "yt-dlp",
                url,
                "-f", "bestaudio/best",
                "--no-playlist",
                "--restrict-filenames",
                "--force-ipv4",
                "-o", template,
                "-v"
            ], capture_output=True, text=True)

            if proc2.returncode != 0:
                print("=== yt-dlp STDERR (fallback) ===")
                print(proc2.stderr)
                await context.bot.send_message(chat_id=chat_id, text="❌ Error al descargar audio (fallback). Revisa logs del contenedor.")
                return

        # Busca MP3 primero; si no, M4A/WebM
        for pat in ["*.mp3", "*.m4a", "*.webm"]:
            matches = sorted(glob.glob(os.path.join(DOWNLOADS_DIR, pat)), key=os.path.getmtime, reverse=True)
            if matches:
                final_file = matches[0]
                try:
                    with open(final_file, "rb") as f:
                        await context.bot.send_audio(chat_id=chat_id, audio=f)
                finally:
                    await manejar_eliminacion_segura(final_file)
                return

        await context.bot.send_message(chat_id=chat_id, text="❌ No se generó ningún archivo de audio.")

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error descargando audio: {e}")


async def descargar_video_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    # Plantilla con extensión dinámica
    template = os.path.join(DOWNLOADS_DIR, "youtube.%(ext)s")
    descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
    try:
        proc = subprocess.run([
            "yt-dlp",
            "-f", "bv*+ba/b",            # mejor video+audio; si no, best
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--restrict-filenames",
            "--force-ipv4",
            "-o", template,
            url
        ], capture_output=True, text=True)

        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            await context.bot.send_message(chat_id=chat_id, text="❌ YouTube error (formato/merge). Revisa logs del contenedor.")
            return

        # Busca cualquier archivo youtube.* recién generado (mp4 ideal)
        for ext in ["mp4", "mkv", "webm"]:
            final_file = os.path.join(DOWNLOADS_DIR, f"youtube.{ext}")
            if os.path.exists(final_file):
                try:
                    with open(final_file, 'rb') as f:
                        await context.bot.send_video(chat_id=chat_id, video=f)
                finally:
                    await manejar_eliminacion_segura(final_file)
                break
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se encontró el archivo de video descargado.")

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ YouTube error: {e}")
    finally:
        try: await descargando_msg.delete()
        except: pass


# =========================
# Handlers
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    url_match = re.search(r"https?://\S+", text)
    if not url_match:
        return

    url = url_match.group(0)
    plataforma = detectar_plataforma(url)

    procesando_msg = await update.message.reply_text("🔎 Procesando...")

    teclado = await obtener_teclado_odesli(url)
    if teclado:
        await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)

    # YouTube: ofrecer Audio/Video
    if plataforma == "youtube":
        link_id = str(uuid.uuid4())
        pending_youtube_links[link_id] = url
        botones = [[
            InlineKeyboardButton("🎬 Video", callback_data=f"ytvideo|{link_id}|{chat_id}"),
            InlineKeyboardButton("🎵 Audio", callback_data=f"ytaudio|{link_id}|{chat_id}")
        ]]
        reply_markup = InlineKeyboardMarkup(botones)
        await update.message.reply_text("¿Qué formato deseas recibir?", reply_markup=reply_markup)
        try: await procesando_msg.delete()
        except: pass
        return

    # Tracks
    if plataforma == "spotify_track":
        query_txt = obtener_metadatos_spotify_track(url)
        if not query_txt:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se pudo extraer título/artista de Spotify.")
            try: await procesando_msg.delete()
            except: pass
            return
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text=f"Descargando: {query_txt}")
        await buscar_y_descargar(query_txt, chat_id, context)
        try: await procesando_msg.delete()
        except: pass
        try: await descargando_msg.delete()
        except: pass
        return

    if plataforma in ["apple_song", "youtube_music"]:
        query_txt = await obtener_metadatos_general(url)
        if not query_txt:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se pudo extraer título/artista.")
            try: await procesando_msg.delete()
            except: pass
            return
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text=f"Descargando: {query_txt}")
        await buscar_y_descargar(query_txt, chat_id, context)
        try: await procesando_msg.delete()
        except: pass
        try: await descargando_msg.delete()
        except: pass
        return

    # Álbumes de Spotify (carátula primero)
    if plataforma == "spotify_album":
        album_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Descargando álbum, esto puede tardar varios minutos...")
        tracks, cover_url, album_name = obtener_tracks_album_spotify(url)
        if not tracks:
            await context.bot.send_message(chat_id=chat_id, text="❌ No pude obtener las canciones del álbum.")
            try: await procesando_msg.delete()
            except: pass
            try: await album_msg.delete()
            except: pass
            return

        # Enviar carátula primero (si existe)
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

    # SoundCloud
    if plataforma == "soundcloud":
        try:
            subprocess.run(["scdl", "-l", limpiar_url_params(url), "-o", DOWNLOADS_DIR, "-f", "--onlymp3"], check=True)
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
        return

    # Twitter / X (video)
    if plataforma == "twitter":
        template = os.path.join(DOWNLOADS_DIR, "x.%(ext)s")
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
        try:
            proc = subprocess.run([
                "yt-dlp",
                "-f", "bv*+ba/b",
                "--merge-output-format", "mp4",
                "--no-playlist",
                "--restrict-filenames",
                "--force-ipv4",
                "-o", template,
                url
            ], capture_output=True, text=True)

            if proc.returncode != 0:
                print(proc.stdout)
                print(proc.stderr)
                await update.message.reply_text("❌ Twitter/X error (formato/merge). Revisa logs del contenedor.")
                return

            for ext in ["mp4", "mkv", "webm"]:
                final_file = os.path.join(DOWNLOADS_DIR, f"x.{ext}")
                if os.path.exists(final_file):
                    try:
                        with open(final_file, 'rb') as f:
                            await context.bot.send_video(chat_id=chat_id, video=f)
                    finally:
                        await manejar_eliminacion_segura(final_file)
                    break
            else:
                await update.message.reply_text("❌ No se encontró el archivo de video descargado.")
        except Exception as e:
            await update.message.reply_text(f"❌ Twitter/X error: {e}")
        finally:
            try: await procesando_msg.delete()
            except: pass
            try: await descargando_msg.delete()
            except: pass
        return

    # No soportado
    await context.bot.send_message(chat_id=chat_id, text="Enlace no soportado aún.")
    try: await procesando_msg.delete()
    except: pass


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Solo recibe link_id y chat_id
    if data.startswith("ytvideo|") or data.startswith("ytaudio|"):
        tipo, link_id, chat_id = data.split("|", 2)
        url = pending_youtube_links.get(link_id)
        if not url:
            await context.bot.send_message(chat_id=int(chat_id), text="❌ Enlace expirado o no encontrado.")
            return

        if tipo == "ytvideo":
            await descargar_video_youtube(url, int(chat_id), context)

        elif tipo == "ytaudio":
            descargando_msg = await context.bot.send_message(chat_id=int(chat_id), text="Descargando audio...")
            await descargar_audio_desde_url(url, int(chat_id), context)
            try: await descargando_msg.delete()
            except: pass

        # Limpia el link de memoria
        pending_youtube_links.pop(link_id, None)


# =========================
# Main + chequeo dependencias
# =========================
def _check_dependencies():
    def _run(cmd):
        try:
            out = subprocess.check_output(cmd, text=True).strip().splitlines()[0]
            print(f"$ {' '.join(cmd)} -> {out}")
        except Exception as e:
            print(f"[WARN] No pude ejecutar {' '.join(cmd)}: {e}")

    _run(["yt-dlp", "--version"])
    _run(["ffmpeg", "-version"])


if __name__ == "__main__":
    _check_dependencies()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot listo. Esperando mensajes...")
    app.run_polling()
