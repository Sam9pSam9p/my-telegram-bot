import logging
import os
import aiohttp

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Берём токен из переменной окружения Railway
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "💎 Отправь адрес токена (Sol/ETH/Base/BNB):\n"
        "пример: So11111111111111111111111111111111111111112\n\n"
        "/price — покажу цену Bitcoin"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    await update.message.reply_text(f"🔍 Анализирую {address[:12]}...")

    async with aiohttp.ClientSession() as session:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        async with session.get(url) as resp:
            data = await resp.json()

    if data.get("pairs"):
        pair = data["pairs"][0]
        price = pair.get("priceUsd", "N/A")
        volume = pair.get("volume", {}).get("h24", 0)
        symbol = pair["baseToken"]["symbol"]
        mcap = pair.get("mcap", 0)

        text = (
            f"💎 {symbol}\n"
            f"💰 Цена: ${price}\n"
            f"📊 Объём 24ч: ${volume:,.0f}\n"
            f"🏦 Market Cap: ${mcap:,.0f}\n"
            f"🔗 {pair['url']}"
        )
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("❌ Токен не найден. Проверь адрес!")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd"
        ) as resp:
            data = await resp.json()
    btc_price = data["bitcoin"]["usd"]
    await update.message.reply_text(f"₿ Bitcoin: ${btc_price:,}")


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден. Проверь переменные Railway.")
        raise SystemExit("BOT_TOKEN is missing")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("🚀 Бот запущен (polling)…")
    application.run_polling()


if __name__ == "__main__":
    main()
