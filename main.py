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

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "💎 Отправь адрес токена (Sol/ETH/Base/BNB)\n"
        "/price — цена Bitcoin"
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

        volume_info = pair.get("volume") or {}
        volume_24h = volume_info.get("h24", 0) or 0

        # MCAP может быть и в корне пары, и внутри "fullyDilutedValuation" / "fdv"
        mcap = pair.get("marketCap") or pair.get("mcap") or 0
        # На некоторых парах MCAP нет — тогда считаем сами, если есть fdv
        if not mcap:
            fdv = pair.get("fdv") or 0
            if fdv:
                mcap = fdv

        symbol = pair["baseToken"]["symbol"]

        text = (
            f"💎 {symbol}\n"
            f"💰 Цена: ${price}\n"
            f"📊 Объём 24ч: ${volume_24h:,.0f}\n"
            f"🏦 MCAP: ${mcap:,.0f}\n"
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
        logger.error("BOT_TOKEN не найден. Проверь переменную в Railway.")
        raise SystemExit("BOT_TOKEN is missing")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("🚀 Бот запущен (polling)…")
    app.run_polling()


if __name__ == "__main__":
    main()
