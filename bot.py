import os
import logging
import subprocess
import requests
import re
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configuración del logger
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")

# ----------------------------
# Funciones auxiliares
# ----------------------------

def extract_artist_name_from_url(url: str) -> str:
    """Extrae nombre de artista desde un link de Apple/Spotify si es posible"""
    if "music.apple.com" in url and "/artist/" in url:
        try:
            return url.split("/artist/")[1].split("/")[0].replace("-", " ")
        except:
            return None
    if "open.spotify.com/artist/" in url:
        return None  # Spotify necesita búsqueda posterior por API o scraping
    return None

def get_spotify_artist_link(artist_name: str) -> str:
    """Intenta obtener el link directo de un artista en Spotify desde su nombre"""
    try:
        search_url = f"https://open.spotify.com/search/{quote(artist_name)}"
        resp = requests.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if resp.status_code == 200:
            match = re.search(r'/artist/([a-zA-Z0-9]+)"', resp.text)
            if match:
                artist_id = match.group(1)
                return f"https://open.spotify.com/artist/{artist_id}"
    except Exception as e:
        logger.error(f"Error buscando artista en Spotify: {e}")
    return f"https://open.spotify.com/search/{quote(artist_name)}"

def build_artist_keyboard(artist_name: str, apple_url: str = None) -> InlineKeyboardMarkup:
    """Construye botones de plataformas para un artista"""
    spotify_url = get_spotify_artist_link(artist_name)
    yt_music_url = f"https://music.youtube.com/search?q={quote(artist_name)}"
    youtube_url = f"https://www.youtube.com/results?search_query={quote(artist_name)}+artist"
    soundcloud_url = f"https://soundcloud.com/search/people?q={quote(artist_name)}"
    deezer_url = f"https://www.deezer.com/search/{quote(artist_name)}"
    tidal_url = f"https://tidal.com/browse/search/{quote(artist_name)}"

    buttons = [
        [
            InlineKeyboardButton("🎵 Spotify", url=spotify_url),
            InlineKeyboardButton("▶️ YT Music", url=yt_music_url),
        ],
        [
            InlineKeyboardButton("📺 YouTube", url=youtube_url),
            InlineKeyboardButton("☁️ SoundCloud", url=soundcloud_url),
        ],
    ]
    if apple_url:
        buttons.append([InlineKeyboardButton("🍏 Apple Music", url=apple_url)])
    buttons.append(
        [
            InlineKeyboardButton("🎶 Deezer", url=deezer_url),
            InlineKeyboardButton("🌊 Tidal", url=tidal_url),
        ]
    )

    return InlineKeyboardMarkup(buttons)

# ----------------------------
# Handlers
# ----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Envíame un link de YouTube, Spotify, Apple Music, SoundCloud o Instagram.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info(f"Mensaje recibido: {text}")

    # --- Detección de artista ---
    if "music.apple.com" in text and "/artist/" in text:
        artist_name = extract_artist_name_from_url(text)
        if artist_name:
            kb = build_artist_keyboard(artist_name, apple_url=text)
            await update.message.reply_text(f"👤 Artista detectado: *{artist_name.title()}*", parse_mode="Markdown", reply_markup=kb)
            return

    if "open.spotify.com/artist/" in text:
        kb = build_artist_keyboard("Artista", apple_url=None)
        await update.message.reply_text("👤 Artista detectado en Spotify", reply_markup=kb)
        return

    # --- Aquí seguiría tu flujo normal para canciones (yt-dlp, spotdl, etc.) ---
    await update.message.reply_text("⚠️ Aún no reconozco ese enlace. ¿Es un track, álbum o artista?")

# ----------------------------
# Main
# ----------------------------

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()

if __name__ == "__main__":
    main()
