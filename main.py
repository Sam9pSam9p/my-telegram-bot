import os
import time
import logging
import asyncio
from collections import deque

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from dexscreener_service import (
    get_token_pairs_by_address,
    pick_best_pair,
)

# ------------------ НАСТРОЙКИ ------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# address -> {"last_checks": deque[(ts, vol24h)], "last_alert": float, "subscribers": set[int]}
tracked_tokens: dict[str, dict] = {}


# ------------------ УТИЛИТЫ ------------------

def check_anomalies(history: deque[tuple[float, float]]):
    """
    history: deque[(timestamp, volume24h)]
    Возвращает список строк-описаний аномалий.
    Аномалия = изменение объёма ≥ 20% на окнах 5s–24h.
    """
    if len(history) < 2:
        return []

    now_ts, last_vol = history[-1]
    alerts: list[str] = []

    # таймфреймы в секундах
    windows = [
        ("5s", 5),
        ("15s", 15),
        ("30s", 30),
        ("60s", 60),
        ("5m", 5 * 60),
        ("15m", 15 * 60),
        ("1h", 60 * 60),
        ("4h", 4 * 60 * 60),
        ("24h", 24 * 60 * 60),
    ]

    for label, span in windows:
        # ищем первое значение старше span
        old_vol = None
        for ts, vol in history:
            if now_ts - ts >= span:
                old_vol = vol
                break

        if old_vol is None or old_vol <= 0:
            continue

        change = (last_vol - old_vol) / old_vol * 100
        if abs(change) >= 20:
            direction = "⬆️" if change > 0 else "⬇️"
            alerts.append(f"{direction} {label}: {change:.1f}% (объём 24h)")

    return alerts


# ------------------ ХЕНДЛЕРЫ КОМАНД ------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "💎 Отправь адрес токена (Sol/ETH/Base/BNB):\n"
        "пример: So11111111111111111111111111111111111111112\n\n"
        "/price — цена Bitcoin"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd"
        ) as resp:
            data = await resp.json()
    btc_price = data["bitcoin"]["usd"]
    await update.message.reply_text(f"₿ Bitcoin: ${btc_price:,}")


# ------------------ ОБРАБОТКА СООБЩЕНИЙ (КОНТРАКТ) ------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    await update.message.reply_text(f"🔍 Анализирую {address[:12]}...")

    async with aiohttp.ClientSession() as session:
        raw = await get_token_pairs_by_address(session, address)

    pair = pick_best_pair(raw)

    if pair:
        price = pair.get("priceUsd", "N/A")

        volume_info = pair.get("volume") or {}
        volume_24h = volume_info.get("h24", 0) or 0

        mcap = pair.get("marketCap") or pair.get("mcap") or 0
        fdv = pair.get("fdv") or 0
        if not mcap and fdv:
            mcap = fdv

        symbol = pair["baseToken"]["symbol"]

        text = (
            f"💎 {symbol}\n"
            f"💰 Цена: ${price}\n"
            f"📊 Объём 24ч: ${volume_24h:,.0f}\n"
            f"🏦 MCAP: ${mcap:,.0f}\n"
            f"🔗 {pair['url']}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🛰 Следить за объёмом", callback_data=f"track:{address}"
                    )
                ]
            ]
        )

        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text("❌ Токен не найден. Проверь адрес!")


# ------------------ КНОПКИ ------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data.startswith("track:"):
        address = data.split(":", 1)[1]
        user_id = query.from_user.id

        info = tracked_tokens.get(address)
        if not info:
            info = {
                "last_checks": deque(maxlen=500),
                "last_alert": 0.0,
                "subscribers": set(),
            }
            tracked_tokens[address] = info

        info["subscribers"].add(user_id)

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ Взял {address[:12]}... на контроль объёмов.\n"
            f"Интервал опроса ~5 секунд, алерты при изменении объёма ≥ 20% "
            f"на окнах 5s–24h."
        )


# ------------------ ФОНОВЫЙ МОНИТОРИНГ ------------------

async def volume_watcher(app: Application):
    """
    Каждые ~5 секунд обходит все отслеживаемые адреса,
    тянет объём 24h и считает аномалии.
    """
    while True:
        if not tracked_tokens:
            await asyncio.sleep(5)
            continue

        async with aiohttp.ClientSession() as session:
            for address, info in list(tracked_tokens.items()):
                try:
                    raw = await get_token_pairs_by_address(session, address)
                    pair = pick_best_pair(raw)
                    if not pair:
                        continue

                    volume_info = pair.get("volume") or {}
                    volume_24h = float(volume_info.get("h24", 0) or 0)

                    now_ts = time.time()
                    history: deque = info["last_checks"]
                    history.append((now_ts, volume_24h))

                    alerts = check_anomalies(history)

                    if alerts and now_ts - info["last_alert"] > 30:
                        info["last_alert"] = now_ts
                        symbol = pair["baseToken"]["symbol"]
                        msg = f"🚨 Аномалия объёма по {symbol}\n" + "\n".join(alerts)

                        for uid in list(info["subscribers"]):
                            try:
                                await app.bot.send_message(chat_id=uid, text=msg)
                            except Exception as e:
                                logger.warning(f"Send alert error: {e}")

                except Exception as e:
                    logger.warning(f"Volume watcher error for {address}: {e}")

        await asyncio.sleep(5)


# ------------------ ЗАПУСК ПРИЛОЖЕНИЯ ------------------

async def run():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден. Проверь переменную в Railway.")
        raise SystemExit("BOT_TOKEN is missing")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    # запускаем фонового вочера
    asyncio.create_task(volume_watcher(app))

    logger.info("🚀 Бот запущен с volume watcher…")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(run())
