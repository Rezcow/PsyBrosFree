import os
import subprocess
import asyncio
import re
from telegram import Update
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


def contiene_enlace_valido(text: str) -> bool:
    enlaces_validos = [
        "youtube.com", "youtu.be", "spotify.com",
        "soundcloud.com", "instagram.com", "x.com", "twitter.com"
    ]
    return any(dominio in text for dominio in enlaces_validos)


def limpiar_url_soundcloud(url: str) -> str:
    return re.sub(r"\?.*", "", url)


async def manejar_eliminacion_segura(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"No se pudo eliminar el archivo: {path}. Error: {e}")


# --- Manejo de mensajes ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if not text or not contiene_enlace_valido(text):
        return

    print(f"\U0001F4E9 Mensaje recibido: {text}")

    # --- YouTube ---
    if "youtube.com" in text or "youtu.be" in text:
        filename = os.path.join(DOWNLOADS_DIR, "youtube_video.mp4")
        try:
            await update.message.reply_text("📹 Descargando video de YouTube...")
            subprocess.run(["yt-dlp", "--no-playlist", "-f", "mp4", "-o", filename, text], check=True)
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(chat_id=chat_id, video=video_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde YouTube:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

    # --- Spotify (buscando en YouTube) ---
    elif "spotify.com" in text:
        try:
            await update.message.reply_text("🎵 Descargando desde Spotify...")
            filename = os.path.join(DOWNLOADS_DIR, f"spotify_{os.getpid()}.mp3")
            subprocess.run(["spotdl", text, "--output", filename], check=True)
            with open(filename, 'rb') as audio_file:
                await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde Spotify:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

    # --- SoundCloud ---
    elif "soundcloud.com" in text:
        try:
            await update.message.reply_text("🎶 Descargando desde SoundCloud...")
            url_limpia = limpiar_url_soundcloud(text)
            subprocess.run(["scdl", "-l", url_limpia, "-o", DOWNLOADS_DIR, "-f", "--onlymp3"], check=True)
            for file in os.listdir(DOWNLOADS_DIR):
                if file.endswith(".mp3"):
                    path = os.path.join(DOWNLOADS_DIR, file)
                    with open(path, 'rb') as audio_file:
                        await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    await manejar_eliminacion_segura(path)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde SoundCloud:\n{str(e)}")

    # --- Instagram ---
    elif "instagram.com" in text:
        filename = os.path.join(DOWNLOADS_DIR, "insta_video.mp4")
        try:
            await update.message.reply_text("📸 Descargando desde Instagram...")
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, text], check=True)
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(chat_id=chat_id, video=video_file)
        except Exception as e:
            await update.message.reply_text(f"❌ Error en descarga desde Instagram:\n{str(e)}")
        finally:
            await manejar_eliminacion_segura(filename)

    # --- X / Twitter ---
    elif "x.com" in text or "twitter.com" in text:
        filename = os.path.join(DOWNLOADS_DIR, "twitter_video.mp4")
        try:
            await update.message.reply_text("🐦 Descargando desde X (Twitter)...")
            subprocess.run(["yt-dlp", "-f", "mp4", "-o", filename, text], check=True)
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
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        print("🛑 Bot detenido por el usuario.")
