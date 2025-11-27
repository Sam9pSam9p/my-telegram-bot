import os
import time
import logging
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

# ------------ НАСТРОЙКИ ------------

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# address -> {"last_checks": deque[(ts, vol_m5)], "last_alert": float, "subscribers": set[int]}
tracked_tokens: dict[str, dict] = {}


# ------------ УТИЛИТЫ ------------

def check_anomalies(history: deque[tuple[float, float]]):
    """
    Возвращает список строк с аномалиями.
    Теперь в history лежит volume.m5 (объём за 5 минут) в динамике,
    и мы считаем изменение этого значения на окнах 5s–24h.
    Порог: |Δ| ≥ 20%.
    """
    if len(history) < 2:
        return []

    now_ts, last_val = history[-1]
    alerts: list[str] = []

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
        old_val = None
        for ts, val in history:
            if now_ts - ts >= span:
                old_val = val
                break

        if old_val is None or old_val <= 0:
            continue

        change = (last_val - old_val) / old_val * 100
        if abs(change) >= 20:
            direction = "⬆️" if change > 0 else "⬇️"
            alerts.append(f"{direction} {label}: {change:.1f}% (volume.m5)")

    return alerts


# ------------ КОМАНДЫ ------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "💎 Отправь адрес токена (Sol/ETH/Base/BNB):\n"
        "пример: So11111111111111111111111111111111111111112\n\n"
        "/price — цена Bitcoin\n"
        "/watchlist — список отслеживаемых\n"
        "/unwatch <адрес> — убрать из отслеживания"
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


# ------------ ОБРАБОТКА КОНТРАКТА ------------

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
        volume_m5 = volume_info.get("m5", 0) or 0  # новый, более «живой» объём[web:93]

        mcap = pair.get("marketCap") or pair.get("mcap") or 0
        fdv = pair.get("fdv") or 0
        if not mcap and fdv:
            mcap = fdv

        symbol = pair["baseToken"]["symbol"]

        text = (
            f"💎 {symbol}\n"
            f"💰 Цена: ${price}\n"
            f"📊 Объём 24ч: ${volume_24h:,.0f}\n"
            f"🕒 Объём 5m: ${volume_m5:,.0f}\n"
            f"🏦 MCAP: ${mcap:,.0f}\n"
            f"🔗 {pair['url']}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🛰 Следить за объёмом (m5)", callback_data=f"track:{address}"
                    )
                ]
            ]
        )

        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text("❌ Токен не найден. Проверь адрес!")


# ------------ КНОПКИ ------------

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
                "last_checks": deque(maxlen=500),  # [(ts, volume_m5)]
                "last_alert": 0.0,
                "subscribers": set(),
            }
            tracked_tokens[address] = info

        info["subscribers"].add(user_id)

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ Взял {address[:12]}... на контроль объёма m5.\n"
            f"Интервал опроса ~5 секунд, алерты при изменении volume.m5 ≥ 20% "
            f"на окнах 5s–24h."
        )


# ------------ СПИСОК / ОТКЛЮЧЕНИЕ ------------

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    user_tokens = []
    for address, info in tracked_tokens.items():
        if user_id in info["subscribers"]:
            user_tokens.append(address)

    if not user_tokens:
        await update.message.reply_text("👀 Сейчас ты ничего не отслеживаешь.")
        return

    text = "🛰 Ты отслеживаешь:\n" + "\n".join(f"- `{addr}`" for addr in user_tokens)
    await update.message.reply_text(text, parse_mode="Markdown")


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Используй: /unwatch <адрес_контракта>")
        return

    address = context.args[0].strip()

    info = tracked_tokens.get(address)
    if not info or user_id not in info["subscribers"]:
        await update.message.reply_text("❌ Этот адрес ты сейчас не отслеживаешь.")
        return

    info["subscribers"].discard(user_id)
    if not info["subscribers"]:
        tracked_tokens.pop(address, None)

    await update.message.reply_text(f"✅ Отключил отслеживание для {address[:12]}...")


# ------------ ФОНОВЫЙ МОНИТОР ------------

async def volume_watcher(app: Application):
    while True:
        logger.info("VOLUME_WATCHER_TICK")
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
                    volume_m5 = float(volume_info.get("m5", 0) or 0)

                    now_ts = time.time()
                    history: deque = info["last_checks"]
                    history.append((now_ts, volume_m5))

                    alerts = check_anomalies(history)

                    if alerts and now_ts - info["last_alert"] > 30:
                        info["last_alert"] = now_ts
                        symbol = pair["baseToken"]["symbol"]
                        msg = f"🚨 Аномалия объёма (m5) по {symbol}\n" + "\n".join(alerts)

                        for uid in list(info["subscribers"]):
                            try:
                                await app.bot.send_message(chat_id=uid, text=msg)
                            except Exception as e:
                                logger.warning(f"Send alert error: {e}")
                except Exception as e:
                    logger.warning(f"Volume watcher error for {address}: {e}")

        await asyncio.sleep(5)


async def post_init(app: Application):
    app.create_task(volume_watcher(app))
    logger.info("🚀 Volume watcher запущен…")


# ------------ MAIN ------------

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден. Проверь переменную в Railway.")
        raise SystemExit("BOT_TOKEN is missing")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🚀 Бот запущен…")
    app.run_polling()


if __name__ == "__main__":
    main()
