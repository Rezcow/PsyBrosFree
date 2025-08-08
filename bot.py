import os
import re
import subprocess
import httpx
import uuid
import random
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# Instaloader (opcional; fallback final para Instagram)
try:
    import instaloader
except Exception:
    instaloader = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# === ENV / Constantes ===
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPOTIPY_CLIENT_ID = os.environ["SPOTIPY_CLIENT_ID"]
SPOTIPY_CLIENT_SECRET = os.environ["SPOTIPY_CLIENT_SECRET"]
INSTAGRAM_SESSIONID = os.environ.get("INSTAGRAM_SESSIONID")  # cookie de sesión IG (opcional)

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

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
    # 1) Intento scrape og:title / og:description
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

    # 2) Intento Songlink/Odesli
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
            err = (proc.stderr or "").strip()
            await context.bot.send_message(chat_id=chat_id, text=f"❌ No se generó archivo de audio.\n{err[:400]}")
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

# === Fallback con Instaloader para Instagram (fotos/carrusel/video) ===
async def descargar_instagram_con_instaloader(url: str, outdir: str):
    """
    Descarga el/los medios de un post de Instagram usando Instaloader.
    Devuelve una lista: [{"path": str, "type": "photo"|"video"}] o [] si falla.
    """
    if instaloader is None:
        print("[Instaloader] no instalado — agrega 'instaloader' a requirements.txt")
        return []

    m = re.search(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return []
    shortcode = m.group(1)

    target_dir = os.path.join(outdir, f"ig_{shortcode}")
    os.makedirs(target_dir, exist_ok=True)

    ua = random.choice(USER_AGENTS)
    L = instaloader.Instaloader(
        dirname_pattern=target_dir,
        filename_pattern=f"insta_{shortcode}",
        download_video_thumbnails=False,
        save_metadata=False,
        max_connection_attempts=3
    )

    try:
        L.context._session.headers.update({"User-Agent": ua})
        if INSTAGRAM_SESSIONID:
            L.context._session.cookies.set("sessionid", INSTAGRAM_SESSIONID, domain=".instagram.com")
    except Exception:
        pass

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target="")
    except Exception as e:
        print(f"[Instaloader] Error: {e}")
        try:
            for f in os.listdir(target_dir):
                await manejar_eliminacion_segura(os.path.join(target_dir, f))
            os.rmdir(target_dir)
        except Exception:
            pass
        return []

    # Recolectar todos los medios (fotos/videos) descargados
    media = []
    try:
        for f in sorted(os.listdir(target_dir)):
            full = os.path.join(target_dir, f)
            if f.lower().endswith(".mp4") and os.path.getsize(full) > 0:
                media.append({"path": full, "type": "video"})
            elif f.lower().endswith((".jpg", ".jpeg", ".png")) and os.path.getsize(full) > 0:
                media.append({"path": full, "type": "photo"})
    except Exception as e:
        print(f"[Instaloader] List media error: {e}")

    return media

# === Handlers ===
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
        await update.message.reply_text("¿Qué formato deseas recibir?", reply_markup=reply_markup)
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
        tmp_id = str(uuid.uuid4())[:8]
        filename = os.path.join(DOWNLOADS_DIR, f"insta_{tmp_id}.mp4")
        descargando_msg = await context.bot.send_message(chat_id=chat_id, text="Descargando media de Instagram...")
        debug_steps = []

        def build_args(target_url: str):
            ua = random.choice(USER_AGENTS)
            args = [
                "yt-dlp",
                "-R", "3",
                "--fragment-retries", "3",
                "--user-agent", ua,
                "--add-header", "Referer: https://www.instagram.com/",
                "-f", "mp4",
                "-o", filename,
                target_url
            ]
            if INSTAGRAM_SESSIONID:
                args.extend(["--add-header", f"Cookie: sessionid={INSTAGRAM_SESSIONID}"])
            return args

        async def intentar_con_ytdlp(u: str, outfile: str):
            try:
                proc = subprocess.run(build_args(u), capture_output=True, text=True)
                exists_ok = os.path.exists(outfile) and os.path.getsize(outfile) > 0
                if proc.returncode != 0 or not exists_ok:
                    err = (proc.stderr or "").strip()
                    try:
                        if os.path.exists(outfile) and os.path.getsize(outfile) == 0:
                            os.remove(outfile)
                    except:
                        pass
                    return False, err[:4000]
                return True, None
            except Exception as e:
                return False, str(e)

        # 1) yt-dlp directo
        ok, err = await intentar_con_ytdlp(url, filename)
        debug_steps.append(f"yt-dlp: {'OK' if ok else 'FAIL'}")

        # 2) ddinstagram con yt-dlp
        if not ok and "instagram.com" in url:
            alt = url.replace("instagram.com", "ddinstagram.com")
            ok2, err2 = await intentar_con_ytdlp(alt, filename)
            debug_steps.append(f"ddinstagram: {'OK' if ok2 else 'FAIL'}")
            if ok2:
                ok, err = ok2, None
            else:
                err = (err or "") + ("\n" + (err2 or ""))

        # 3) Instaloader (fotos/carrusel/video)
        used_instaloader = False
        ig_media = []
        if not ok:
            if instaloader is None:
                debug_steps.append("instaloader: NOT_INSTALLED")
            else:
                ig_media = await descargar_instagram_con_instaloader(url, DOWNLOADS_DIR)
                used_instaloader = bool(ig_media)
                debug_steps.append(f"instaloader: {'OK' if used_instaloader else 'FAIL'}")
                if used_instaloader:
                    ok = True

        try:
            if ok and not used_instaloader and os.path.exists(filename):
                # Caso yt-dlp / ddinstagram con video
                with open(filename, 'rb') as f:
                    await context.bot.send_video(chat_id=chat_id, video=f)

            elif ok and used_instaloader and ig_media:
                # Enviar 1 o varios medios
                if len(ig_media) == 1:
                    item = ig_media[0]
                    if item["type"] == "video":
                        with open(item["path"], "rb") as f:
                            await context.bot.send_video(chat_id=chat_id, video=f)
                    else:
                        with open(item["path"], "rb") as f:
                            await context.bot.send_photo(chat_id=chat_id, photo=f)
                else:
                    # Enviar como álbum (media group), máx 10 elementos por mensaje
                    batch = []
                    for m in ig_media[:10]:
                        if m["type"] == "video":
                            batch.append(InputMediaVideo(open(m["path"], "rb")))
                        else:
                            batch.append(InputMediaPhoto(open(m["path"], "rb")))
                    await context.bot.send_media_group(chat_id=chat_id, media=batch)

            else:
                hint = ""
                if err and any(k in err.lower() for k in ["login", "private", "login required", "forbidden", "not available"]):
                    hint = "\n⚠️ Tip: agrega/actualiza INSTAGRAM_SESSIONID en variables de entorno."
                steps = " · ".join(debug_steps) if debug_steps else "no-steps"
                await update.message.reply_text(f"❌ Instagram error.\nRutas: {steps}\n{(err or 'Sin detalle')[:700]}{hint}")

        finally:
            # Limpieza de archivos temporales
            await manejar_eliminacion_segura(filename)
            # limpiar carpeta generada por instaloader si existió
            try:
                if ig_media:
                    base_dir = os.path.dirname(ig_media[0]["path"])
                    for f in os.listdir(base_dir):
                        await manejar_eliminacion_segura(os.path.join(base_dir, f))
                    os.rmdir(base_dir)
            except Exception:
                pass
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

        pending_youtube_links.pop(link_id, None)

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot listo. Esperando mensajes...")
    app.run_polling()
