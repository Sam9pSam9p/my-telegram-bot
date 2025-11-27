import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import aiohttp
import os

# ТОКЕН из переменной окружения Railway!
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "💎 Отправь адрес токена:\n"
        "• Solana: `So111111111...`\n"
        "• ETH/Base: `0x123...`\n\n"
        "📊 /price — цена Bitcoin"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    
    await update.message.reply_text(f"🔍 Анализирую {address[:12]}...")
    
    async with aiohttp.ClientSession() as session:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        async with session.get(url) as resp:
            data = await resp.json()
    
    if data.get('pairs'):
        pair = data['pairs'][0]
        price = pair.get('priceUsd', 'N/A')
        volume = pair.get('volume', {}).get('h24', 0)
        symbol = pair['baseToken']['symbol']
        mcap = pair.get('mcap', 0)
        
        text = f"""💎 {symbol}
💰 Цена: ${price}
📊 Объём 24ч: ${volume:,.0f}
🏦 Market Cap: ${mcap:,.0f}
🔗 {pair['url']}"""
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("❌ Токен не найден. Проверь адрес!")

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd") as resp:
            data = await resp.json()
            btc_price = data['bitcoin']['usd']
            await update.message.reply_text(f"₿ Bitcoin: ${btc_price:,}")

def main():
    if not BOT_TOKEN:
        print("❌ ОШИБКА: BOT_TOKEN не найден!")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 Бот запущен!")
    app.run_polling()

if __name__ == '__main__':
    main()
