import os
import re
import logging
import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("odesli-only-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

def extraer_url(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    # limpia signos de cierre comunes pegados al final
    url = m.group(0).rstrip(").,>]}\"'")
    return url

async def obtener_teclado_odesli(original_url: str):
    api = f"https://api.song.link/v1-alpha.1/links?url={original_url}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(api, timeout=12)
            if r.status_code != 200:
                return None
            data = r.json()
            links = data.get("linksByPlatform", {})
            if not links:
                return None
            botones, fila = [], []
            for nombre, info in links.items():
                url = info.get("url")
                if not url:
                    continue
                fila.append(InlineKeyboardButton(text=nombre.capitalize(), url=url))
                if len(fila) == 3:
                    botones.append(fila); fila = []
            if fila:
                botones.append(fila)
            return InlineKeyboardMarkup(botones) if botones else None
    except Exception as e:
        log.warning(f"Odesli error: {e}")
        return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extraer_url(update.message.text if update.message else "")
    if not url:
        return
    teclado = await obtener_teclado_odesli(url)
    if teclado:
        await update.message.reply_text("🎶 Disponible en:", reply_markup=teclado)
    # si Odesli no devuelve nada, silencio

async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook eliminado; polling limpio.")
    except Exception as e:
        log.warning(f"No pude limpiar webhook: {e}")

if __name__ == "__main__":
    app = (Application.builder().token(BOT_TOKEN).post_init(_post_init).build())
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ Bot Odesli-only listo. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)
