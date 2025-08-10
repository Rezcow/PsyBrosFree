# bot.py — PsyBrosBot (YouTube + Spotify/Apple/YT Music) — clean & Railway-friendly

import os
import re
import uuid
import subprocess
import logging
import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------------- Config & logging ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("psybot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ---- yt-dlp endurecido + cookies opcionales ----
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES_YT")  # contenido Netscape del cookies.txt (opcional)
COOKIES_FILE = None
if YTDLP_COOKIES:
    os.makedirs("cookies", exist_ok=True)
    COOKIES_FILE = "cookies/youtube.txt"
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(YTDLP_COOKIES)
    log.info("Cookies de YouTube cargadas.")

YTDLP_BASE = [
    "--no-warnings",
    "--restrict-filenames",
    "--no-playlist",
    "--add-header", "User-Agent: Mozilla/5.0",
    "--add-header", "Referer: https://www.youtube.com/",
    "--extractor-args", "youtube:player_client=android",  # cliente móvil
    "--geo-bypass",
]
if COOKIES_FILE:
    YTDLP_BASE += ["--cookies", COOKIES_FILE]

# enlaces youtube en espera (para botones)
pending_youtube_links = {}

# ---------------- Utils ----------------
def extraer_url(text: str) -> str | None:
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
    return None  # todo lo demás (instagram, etc) se ignora

async def manejar_eliminacion_segura(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log.warning(f"Error al eliminar {path}: {e}")

async def obtener_teclado_odesli(original_url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={original_url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api_url, timeout=12)
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
        log.warning(f"Odesli error: {e}")
        return None

async def obtener_titulo_artista_con_odesli(url: str):
    api_url = f"https://api.song.link/v1-alpha.1/links?url={url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api_url, timeout=12)
            if r.status_code != 200:
                return None, None
            data = r.json()
            uid = data.get("entityUniqueId")
            entity = data.get("entitiesByUniqueId", {}).get(uid, {}) if uid else {}
            return entity.get("title"), entity.get("artistName")
    except Exception as e:
        log.warning(f"[Odesli meta] {e}")
        return None, None

# ---------------- yt-dlp helpers ----------------
async def _ytdlp_print_path(cmd: list[str]) -> tuple[int, str, str, str]:
    """Ejecuta yt-dlp y devuelve (rc, stdout, stderr, final_path)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out_lines = (proc.stdout or "").strip().splitlines()
    final_path = out_lines[-1] if out_lines else ""
    return proc.returncode, proc.stdout, proc.stderr, final_path

async def buscar_y_descargar(query: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Busca en YouTube y extrae a MP3."""
    template = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp", f"ytsearch1:{query}",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", template, "--print", "after_move:filepath",
    ] + YTDLP_BASE

    rc, so, se, final_path = await _ytdlp_print_path(cmd)

    # Fallback: cambia cliente a iOS si “confirm you’re not a bot”
    if rc != 0 and "confirm you’re not a bot" in (se or "") and "--cookies" not in YTDLP_BASE:
        cmd_ios = cmd[:]
        i = cmd_ios.index("--extractor-args") + 1
        cmd_ios[i] = "youtube:player_client=ios"
        rc, so, se, final_path = await _ytdlp_print_path(cmd_ios)

    if rc == 0 and final_path and os.path.exists(final_path):
        try:
            with open(final_path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=query)
        finally:
            await manejar_eliminacion_segura(final_path)
    else:
        err = (se or "")[:300]
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No se generó audio.\n{err}")

async def descargar_audio_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    template = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp", url,
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", template, "--print", "after_move:filepath",
    ] + YTDLP_BASE

    rc, so, se, final_path = await _ytdlp_print_path(cmd)
    if rc != 0 and "confirm you’re not a bot" in (se or "") and "--cookies" not in YTDLP_BASE:
        cmd_ios = cmd[:]
        i = cmd_ios.index("--extractor-args") + 1
        cmd_ios[i] = "youtube:player_client=ios"
        rc, so, se, final_path = await _ytdlp_print_path(cmd_ios)

    if rc == 0 and final_path and os.path.exists(final_path):
        try:
            with open(final_path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f)
        finally:
            await manejar_eliminacion_segura(final_path)
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ No pude extraer audio.\n{(se or '')[:300]}")

async def descargar_video_youtube(url: str, chat_id, context: ContextTypes.DEFAULT_TYPE):
    filename = os.path.join(DOWNLOADS_DIR, "youtube.mp4")
    cmd = ["yt-dlp", "-f", "mp4", "-o", filename, url] + YTDLP_BASE
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0 and "confirm you’re not a bot" in (proc.stderr or "") and "--cookies" not in YTDLP_BASE:
        cmd_ios = cmd[:]
        i = cmd_ios.index("--extractor-args") + 1
        cmd_ios[i] = "youtube:player_client=ios"
        proc = subprocess.run(cmd_ios, capture_output=True, text=True)

    if proc.returncode == 0 and os.path.exists(filename):
        try:
            with open(filename, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f)
        finally:
            await manejar_eliminacion_segura(filename)
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ YouTube error.\n{(proc.stderr or '')[:300]}")

# ---------------- Handlers ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else ""
    url = extraer_url(text)
    if not url:
        return

    plataforma = plataforma_permitida(url)
    if not plataforma:
        return  # ignorar cualquier otra cosa (instagram, twitter, etc.)

    chat_id = update.effective_chat.id
    procesando = await update.message.reply_text("🔎 Procesando...")

    teclado = await obtener_teclado_odesli(url)
    if teclado:
        await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)

    try:
        if plataforma == "youtube":
            # Botones Audio / Video
            link_id = str(uuid.uuid4())
            pending_youtube_links[link_id] = url
            botones = [[
                InlineKeyboardButton("🎬 Video", callback_data=f"ytvideo|{link_id}|{chat_id}"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"ytaudio|{link_id}|{chat_id}")
            ]]
            await update.message.reply_text("¿Qué formato deseas recibir?", reply_markup=InlineKeyboardMarkup(botones))

        else:  # spotify_track / ytmusic / apple_music
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
        else:
            await context.bot.send_message(chat_id=int(chat_id), text="⏳ Extrayendo audio…")
            await descargar_audio_youtube(url, int(chat_id), context)

        pending_youtube_links.pop(link_id, None)

# Borrar webhook al arrancar (evita conflictos con polling)
async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook eliminado (si había). Polling limpio.")
        # opcional: imprime versiones
        log.info("yt-dlp: %s", subprocess.getoutput("yt-dlp --version"))
        log.info("ffmpeg: %s", subprocess.getoutput("ffmpeg -version").splitlines()[0])
    except Exception as e:
        log.warning(f"No pude limpiar webhook: {e}")

# ---------------- Main ----------------
if __name__ == "__main__":
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    log.info("✅ Bot listo. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)
