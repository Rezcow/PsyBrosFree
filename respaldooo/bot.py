import os
import re
import uuid
import subprocess
import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# TOKEN desde variable de entorno
BOT_TOKEN = os.environ["BOT_TOKEN"]

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Guardamos enlaces YouTube pendientes (id -> url)
pending_youtube_links = {}

# ---------------- Utils ----------------

def extraer_url(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else None

def plataforma_permitida(url: str) -> str | None:
    u = url.lower()
    if "music.youtube.com" in u:
        return "ytmusic"
    if "spotify.com" in u and "/track/" in u:
        return "spotify_track"
    if "music.apple.com" in u and ("/song/" in u or "?i=" in u):
        return "apple_music"
    if "youtu.be" in u or "youtube.com" in u:
        return "youtube"
    return None  # instagram/soundcloud/twitter/etc -> ignorar

async def manejar_eliminacion_segura(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Error al eliminar {path}: {e}")

async def obtener_teclado_odesli(original_url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={original_url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api_url, timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
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

async def obtener_titulo_artista_con_odesli(url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api_url, timeout=10)
            if r.status_code != 200:
                return None, None
            data = r.json()
            uid = data.get("entityUniqueId")
            entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
            return entity.get("title"), entity.get("artistName")
    except Exception as e:
        print(f"[Odesli meta] {e}")
        return None, None

async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Descarga audio por búsqueda en YouTube (MP3, con ffmpeg)."""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", query)
    output_path = os.path.join(DOWNLOADS_DIR, f"{sanitized}.mp3")
    try:
        subprocess.run([
            "yt-dlp",
            f"ytsearch1:{query}",
            "--extract-audio",
            "--audio-format", "mp3",
            "-o", output_path
        ], check=True)

        if os.path.exists(output_path):
            with open(output_path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=query)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se generó archivo de audio.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query} ({e})")
    finally:
        await manejar_eliminacion_segura(output_path)

async def descargar_audio_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Extrae audio MP3 directamente desde un enlace de YouTube."""
    template = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    try:
        proc = subprocess.run([
            "yt-dlp", url,
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-warnings",
            "--restrict-filenames",
            "-o", template,
            "--print", "after_move:filepath"
        ], capture_output=True, text=True, check=True)
        final_path = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if final_path and os.path.exists(final_path):
            try:
                with open(final_path, "rb") as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f)
            finally:
                await manejar_eliminacion_segura(final_path)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se generó archivo de audio.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo extraer audio: {e}")

async def descargar_video_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    filename = os.path.join(DOWNLOADS_DIR, "youtube.mp4")
    try:
        subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
        with open(filename, "rb") as f:
            await context.bot.send_video(chat_id=chat_id, video=f)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ YouTube error: {e}")
    finally:
        await manejar_eliminacion_segura(filename)

# ---------------- Handlers ----------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else ""
    url = extraer_url(text)
    if not url:
        return

    plataforma = plataforma_permitida(url)
    if not plataforma:
        return  # ignorar todo lo demás (Instagram, etc.)

    chat_id = update.effective_chat.id
    procesando = await update.message.reply_text("🔎 Procesando...")

    teclado = await obtener_teclado_odesli(url)
    if teclado:
        await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)

    try:
        if plataforma == "youtube":
            # Preguntar Audio o Video
            link_id = str(uuid.uuid4())
            pending_youtube_links[link_id] = url
            botones = [[
                InlineKeyboardButton("🎬 Video", callback_data=f"ytvideo|{link_id}|{chat_id}"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"ytaudio|{link_id}|{chat_id}")
            ]]
            await update.message.reply_text("¿Qué formato deseas recibir?", reply_markup=InlineKeyboardMarkup(botones))
        elif plataforma in ("spotify_track", "ytmusic", "apple_music"):
            title, artist = await obtener_titulo_artista_con_odesli(url)
            if not title:
                await update.message.reply_text("❌ No se pudo extraer título/artista.")
                return
            query = f"{title} {artist or ''}".strip()
            await update.message.reply_text(f"🔍 Buscando en YouTube: {query}")
            await buscar_y_descargar(query, chat_id, context)
    finally:
        try:
            await procesando.delete()
        except:
            pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("ytvideo|") or data.startswith("ytaudio|"):
        tipo, link_id, chat_id = data.split("|", 2)
        url = pending_youtube_links.get(link_id)
        if not url:
            await context.bot.send_message(chat_id=int(chat_id), text="❌ Enlace expirado o no encontrado.")
            return

        if tipo == "ytvideo":
            await context.bot.send_message(chat_id=int(chat_id), text="⏳ Descargando video…")
            await descargar_video_youtube(url, int(chat_id), context)
        else:  # ytaudio
            await context.bot.send_message(chat_id=int(chat_id), text="⏳ Extrayendo audio…")
            await descargar_audio_youtube(url, int(chat_id), context)

        pending_youtube_links.pop(link_id, None)

# ---------------- Main ----------------

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot listo. Esperando mensajes...")
    app.run_polling()
