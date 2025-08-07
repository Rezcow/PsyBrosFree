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

BOT_TOKEN = "8194406693:AAHgUSR31UV7qrUCZZOhbAJibi2XrxYmads"
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

def extraer_url(text: str) -> str:
    match = re.search(r"https?://[\w./?=&%-]+", text)
    return match.group(0) if match else None

def limpiar_texto(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', '', text)

def contiene_enlace_valido(text: str) -> bool:
    return extraer_url(text) is not None

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

async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    safe_query = limpiar_texto(query)
    filename = os.path.join(DOWNLOADS_DIR, f"{safe_query}.mp3")
    try:
        subprocess.run([
            "yt-dlp", f"ytsearch1:{query}",
            "--extract-audio", "--audio-format", "mp3",
            "-o", filename
        ], check=True)
        if os.path.exists(filename):
            with open(filename, 'rb') as audio_file:
                await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=query)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No se pudo descargar: {query}")
    finally:
        await manejar_eliminacion_segura(filename)

async def procesar_spotify(url, chat_id, context):
    try:
        result = subprocess.run(["yt-dlp", "-j", url], capture_output=True, text=True)
        if result.returncode != 0:
            return await context.bot.send_message(chat_id=chat_id, text="❌ No se pudo procesar URL Spotify.")
        info = json.loads(result.stdout)

        if info.get("entries"):
            await context.bot.send_message(chat_id=chat_id, text="🎵 Álbum o playlist detectado. Descargando pistas...")
            flat = subprocess.run([
                "yt-dlp", "--flat-playlist", "--print", "%(id)s - %(title)s", url
            ], capture_output=True, text=True)
            for line in flat.stdout.strip().splitlines():
                _, t = line.split(" - ", 1)
                await buscar_y_descargar(t.strip(), chat_id, context)
        elif info.get("title"):
            await context.bot.send_message(chat_id=chat_id, text=f"🎵 Obteniendo: {info['title']}")
            await buscar_y_descargar(info['title'], chat_id, context)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ No se obtuvo título de Spotify.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error al procesar Spotify: {e}")

# --- Manejo de mensajes ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if not text or not contiene_enlace_valido(text):
        return

    url = extraer_url(text)
    print(f"📩 Mensaje recibido: {url}")

    if "spotify.com" in url:
        consultando = await update.message.reply_text("🎵 Procesando enlace de Spotify...")
        await procesar_spotify(url, chat_id, context)
        await consultando.delete()
    else:
        teclado = await obtener_teclado_odesli(url)
        if teclado:
            await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)

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
