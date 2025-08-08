import os
import re
import subprocess
import httpx
import uuid
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPOTIPY_CLIENT_ID = os.environ["SPOTIPY_CLIENT_ID"]
SPOTIPY_CLIENT_SECRET = os.environ["SPOTIPY_CLIENT_SECRET"]
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Diccionario para asociar el UUID al link real de YouTube
pending_youtube_links = {}

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

async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    sanitized = re.sub(r'[\\/*?:"<>|]', "", query)
    output_path = os.path.join(DOWNLOADS_DIR, f"{sanitized}.mp3")
    try:
        proc = subprocess.run([
            "yt-dlp",
            f"ytsearch1:{query}",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "9",
            "-o", output_path
        ], capture_output=True, text=True)

        if os.path.exists(output_path):
            with open(output_path, 'rb') as audio_file:
                await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=query)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se generó archivo de audio.")
    except Exception as e:
        if "Timed out" not in str(e):
            await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query} ({e})")
    finally:
        await manejar_eliminacion_segura(output_path)

async def descargar_video_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    filename = os.path.join(DOWNLOADS_DIR, "youtube.mp4")
    descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
    try:
        subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
        with open(filename, 'rb') as f:
            await context.bot.send_video(chat_id=chat_id, video=f)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ YouTube error: {e}")
    finally:
        await manejar_eliminacion_segura(filename)
        try: await descargando_msg.delete()
        except: pass

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
    if "instagram.com" in url:
        return "instagram"
    if "twitter.com" in url or "x.com" in url:
        return "twitter"
    return "desconocido"

def obtener_tracks_album_spotify(album_url):
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

    # YOUTUBE: pregunta si audio o video usando UUID
    if plataforma == "youtube":
        link_id = str(uuid.uuid4())
        pending_youtube_links[link_id] = url
        botones = [
            [
                InlineKeyboardButton("🎬 Video", callback_data=f"ytvideo|{link_id}|{chat_id}"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"ytaudio|{link_id}|{chat_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(botones)
        await update.message.reply_text(
            "¿Qué formato deseas recibir?",
            reply_markup=reply_markup
        )
        try: await procesando_msg.delete()
        except: pass
        return

    # TRACKS: Spotify, Apple Music, YouTube Music (audio)
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

    # ALBUMES SPOTIFY (carátula primero)
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

        # Descargar y enviar carátula primero
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

        for idx, query in enumerate(tracks, 1):
            try:
                descargando_msg = await context.bot.send_message(chat_id=chat_id, text=f"🎵 [{idx}/{len(tracks)}] Descargando: {query}")
                await buscar_y_descargar(query, chat_id, context)
                try: await descargando_msg.delete()
                except: pass
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Error con la canción {query}: {e}")

        await context.bot.send_message(chat_id=chat_id, text="✅ Álbum completo enviado.")
        try: await procesando_msg.delete()
        except: pass
        return

    elif plataforma == "soundcloud":
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

    elif plataforma == "instagram":
        filename = os.path.join(DOWNLOADS_DIR, "insta.mp4")
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
        try:
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
            with open(filename, 'rb') as f:
                await context.bot.send_video(chat_id=chat_id, video=f)
        except Exception as e:
            await update.message.reply_text(f"❌ Instagram error: {e}")
        finally:
            await manejar_eliminacion_segura(filename)
            try: await procesando_msg.delete()
            except: pass
            try: await descargando_msg.delete()
            except: pass

    elif plataforma == "twitter":
        filename = os.path.join(DOWNLOADS_DIR, "x.mp4")
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando video...")
        try:
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Solo recibe el link_id y el chat_id
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
                subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
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

        # Limpia el link de memoria
        pending_youtube_links.pop(link_id, None)

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot listo. Esperando mensajes...")
    app.run_polling()
