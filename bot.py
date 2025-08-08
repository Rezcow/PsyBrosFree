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

pending_youtube_links = {}

INSTAGRAM_RE = re.compile(r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[A-Za-z0-9._/-]+")
TWITTER_RE = re.compile(r"https?://(www\.)?(twitter|x)\.com/[A-Za-z0-9_]+/status/[0-9]+")
YOUTUBE_RE = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be)/\S+")

def limpiar_url_params(url: str) -> str:
    return url.split("?")[0]

def _cookies_path():
    path = os.environ.get("IG_COOKIES", "cookies.txt")
    return path if os.path.exists(path) else None

def _yt_dlp_cmd(url, outtmpl, use_cookies=False):
    cmd = [
        "yt-dlp",
        "-N", "4",
        "--no-warnings",
        "--no-playlist",
        "-o", outtmpl,
        "-S", "res,ext",
        "-f", "bestvideo*+bestaudio/best[ext=mp4]/best",
        url
    ]
    if use_cookies:
        cookies = _cookies_path()
        if cookies:
            cmd = ["yt-dlp", "--cookies", cookies] + cmd[1:]
    return cmd

async def descargar_instagram(update: Update, url: str):
    url = limpiar_url_params(url)
    file_id = str(uuid.uuid4())[:8]
    output_file = os.path.join(DOWNLOADS_DIR, f"insta_{file_id}.mp4")

    cmd = _yt_dlp_cmd(url, output_file, use_cookies=False)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        await update.message.reply_text("✅ Descarga de Instagram exitosa.")
        return output_file
    except subprocess.CalledProcessError as e:
        err_msg = (f"❌ yt-dlp error al descargar Instagram:\n"
                   f"STDERR:\n{e.stderr[:1500]}\n"
                   f"STDOUT:\n{e.stdout[:500]}")
        await update.message.reply_text(err_msg)
        return None

# Puedes replicar este patrón para Twitter, SoundCloud, etc., si quieres debug detallado.

# Placeholder de tus funciones personalizadas (deberás agregar aquí tus funciones originales)
async def obtener_teclado_odesli(original_url: str):
    return None
async def obtener_metadatos_general(url: str):
    return None
async def buscar_y_descargar(update, url):
    return None
async def descargar_video_youtube(update, url):
    return None
async def detectar_plataforma(url: str):
    return None
async def obtener_tracks_album_spotify(url: str):
    return None

# Handler principal de mensajes
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    url = text

    # Instagram
    if INSTAGRAM_RE.search(url):
        await update.message.reply_text("🔄 Descargando de Instagram, por favor espera...")
        file_path = await descargar_instagram(update, url)
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                await update.message.reply_video(f)
            os.remove(file_path)
        return

    # Twitter/X (Placeholder, puedes adaptar el bloque de debug igual que Instagram)
    if TWITTER_RE.search(url):
        await update.message.reply_text("🔄 (Bloque Twitter aquí)")
        return

    # YouTube (Placeholder, puedes adaptar igual)
    if YOUTUBE_RE.search(url):
        await update.message.reply_text("🔄 (Bloque YouTube aquí)")
        return

    # SoundCloud, Spotify, etc. (Placeholders)
    # ... tu lógica aquí

    await update.message.reply_text("Enlace no reconocido o plataforma no soportada aún.")

# Handler para botones (si tienes callbacks)
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Funcionalidad aún no implementada.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("Bot iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()
