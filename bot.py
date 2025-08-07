import os
import subprocess
import asyncio
import re
import json
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Configuración inicial ---
BOT_TOKEN = "8194406693:AAHgUSR31UV7qrUCZZOhbAJibi2XrxYmads"
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# --- Utilidades ---
def extraer_url(text: str) -> str:
    match = re.search(r"https?://[\w./?=&%-]+", text)
    return match.group(0) if match else None

def contiene_enlace_valido(text: str) -> bool:
    return extraer_url(text) is not None

def limpiar_url_soundcloud(url: str) -> str:
    return re.sub(r"\?.*", "", url)

async def manejar_eliminacion_segura(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"No se pudo eliminar el archivo: {path}. Error: {e}")

async def obtener_teclado_odesli(original_url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={original_url}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, timeout=10)
            if response.status_code != 200:
                return None
            data = response.json()
            links = data.get("linksByPlatform", {})

            if not links:
                return None

            botones = []
            fila = []
            for i, (nombre, info) in enumerate(links.items()):
                url = info.get("url")
                if url:
                    boton = InlineKeyboardButton(text=nombre.capitalize(), url=url)
                    fila.append(boton)
                    if len(fila) == 3:
                        botones.append(fila)
                        fila = []
            if fila:
                botones.append(fila)

            return InlineKeyboardMarkup(botones)
    except Exception as e:
        print(f"Error en consulta a Odesli: {e}")
        return None

def es_link_musical(url: str) -> bool:
    plataformas_musicales = [
        "spotify.com", "music.apple.com", "youtube.com", "youtu.be",
        "deezer.com", "tidal.com", "soundcloud.com", "amazon.com/music"
    ]
    return any(p in url for p in plataformas_musicales)

async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    filename = os.path.join(DOWNLOADS_DIR, f"{query}.mp3")
    try:
        subprocess.run([
            "yt-dlp", f"ytsearch1:{query}",
            "--extract-audio", "--audio-format", "mp3",
            "-o", filename
        ], check=True)
        with open(filename, 'rb') as audio_file:
            await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=query)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query}")
    finally:
        await manejar_eliminacion_segura(filename)

# --- Spotify (usando yt-dlp -j) ---
async def manejar_spotify(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=chat_id, text="🎵 Buscando en YouTube equivalente a la canción de Spotify...")
    try:
        result = subprocess.run(
            ["yt-dlp", "-j", url],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        title = data.get("title")
        if title:
            await buscar_y_descargar(title, chat_id, context)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se pudo obtener el título desde Spotify.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error al procesar enlace de Spotify:\n{str(e)}")

# --- Manejo de mensajes ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if not text or not contiene_enlace_valido(text):
        return

    url = extraer_url(text)
    print(f"📩 Mensaje recibido: {url}")

    if es_link_musical(url):
        consultando = await update.message.reply_text("🔍 Consultando enlaces equivalentes...")
        teclado = await obtener_teclado_odesli(url)
        if teclado:
            await consultando.delete()
            await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)
        else:
            await consultando.edit_text("⚠️ No se pudieron encontrar enlaces equivalentes.")

    if "spotify.com" in url:
        await manejar_spotify(url, chat_id, context)

    elif "youtube.com" in url or "youtu.be" in url:
        filename = os.path.join(DOWNLOADS_DIR, "youtube_video.mp4")
        try:
            await update.message.reply_text("🎮 Tu descarga está en camino...")
            subprocess.run(["yt-dlp", "--no-playlist", "-f", "mp4", "-o", filename, url], check=True)
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(chat_id=chat_id, video=video_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde YouTube:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

    elif "soundcloud.com" in url:
        try:
            await update.message.reply_text("🎶 Descargando desde SoundCloud...")
            url_limpia = limpiar_url_soundcloud(url)
            subprocess.run(["scdl", "-l", url_limpia, "-o", DOWNLOADS_DIR, "-f", "--onlymp3"], check=True)
            for file in os.listdir(DOWNLOADS_DIR):
                if file.endswith(".mp3"):
                    path = os.path.join(DOWNLOADS_DIR, file)
                    with open(path, 'rb') as audio_file:
                        await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    await manejar_eliminacion_segura(path)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde SoundCloud:\n{str(e)}")

    elif "instagram.com" in url:
        filename = os.path.join(DOWNLOADS_DIR, "insta_video.mp4")
        try:
            await update.message.reply_text("📸 Descargando desde Instagram...")
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(chat_id=chat_id, video=video_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde Instagram:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

    elif "x.com" in url or "twitter.com" in url:
        filename = os.path.join(DOWNLOADS_DIR, "twitter_video.mp4")
        try:
            await update.message.reply_text("🐦 Descargando desde X (Twitter)...")
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, url], check=True)
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(chat_id=chat_id, video=video_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde X:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

# --- Inicio del bot ---
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot iniciado. Esperando mensajes...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot detenido por el usuario.")