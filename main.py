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

# address -> {
#   "last_checks": deque[(ts, vol_m5)],
#   "last_alert": float,
#   "subscribers": { user_id: {"vol_threshold": float} }
# }
tracked_tokens: dict[str, dict] = {}

# user_id -> {"pending_volume_for": address}  (ждём ввода порога)
pending_threshold_input: dict[int, dict] = {}


# ------------ УТИЛИТЫ ------------

def check_anomalies(
    history: deque[tuple[float, float]],
    user_threshold: float,
):
    """
    Возвращает список строк с аномалиями для конкретного пользователя.
    history: [(timestamp, volume_m5)]
    user_threshold: порог в % (например 20.0)
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
        if abs(change) >= user_threshold:
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


# ------------ ОБРАБОТКА КОНТРАКТА (+ ВВОД ПОРОГА) ------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # 1) Если пользователь сейчас вводит порог для объёма
    state = pending_threshold_input.get(user_id)
    if state and state.get("pending_volume_for"):
        address = state["pending_volume_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи процент, например: 20"
            )
            return

        info = tracked_tokens.get(address)
        if not info or user_id not in info["subscribers"]:
            await update.message.reply_text(
                "❌ Похоже, этот контракт уже не отслеживается. "
                "Нажми кнопку ещё раз, чтобы начать заново."
            )
            pending_threshold_input.pop(user_id, None)
            return

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть больше 0. Попробуй ещё раз."
            )
            return

        info["subscribers"][user_id]["vol_threshold"] = threshold
        pending_threshold_input.pop(user_id, None)

        await update.message.reply_text(
            f"✅ Установлен порог объёма: {threshold:.1f}%.\n"
            f"Алерты будут при изменении volume.m5 на это значение или больше."
        )
        return

    # 2) Обычный режим: считаем, что это контракт
    address = text
    await update.message.reply_text(f"🔍 Анализирую {address[:12]}...")

    async with aiohttp.ClientSession() as session:
        raw = await get_token_pairs_by_address(session, address)

    pair = pick_best_pair(raw)

    if pair:
        price = pair.get("priceUsd", "N/A")

        volume_info = pair.get("volume") or {}
        volume_24h = volume_info.get("h24", 0) or 0
        volume_m5 = volume_info.get("m5", 0) or 0

        mcap = pair.get("marketCap") or pair.get("mcap") or 0
        fdv = pair.get("fdv") or 0
        if not mcap and fdv:
            mcap = fdv

        symbol = pair["baseToken"]["symbol"]

        text_resp = (
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

        await update.message.reply_text(text_resp, reply_markup=keyboard)
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
                "subscribers": {},
            }
            tracked_tokens[address] = info

        # создаём запись подписчика с дефолтным порогом (перезапишем после ввода)
        info["subscribers"].setdefault(user_id, {"vol_threshold": 20.0})

        # помечаем, что ждём от юзера порог
        pending_threshold_input[user_id] = {"pending_volume_for": address}

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "📊 Введи процент изменения объёма m5, при котором слать алерт.\n"
            "Например: 20"
        )


# ------------ СПИСОК / ОТКЛЮЧЕНИЕ ------------

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    user_tokens = []
    for address, info in tracked_tokens.items():
        if user_id in info["subscribers"]:
            thr = info["subscribers"][user_id]["vol_threshold"]
            user_tokens.append(f"{address} (vol ≥ {thr:.1f}%)")

    if not user_tokens:
        await update.message.reply_text("👀 Сейчас ты ничего не отслеживаешь.")
        return

    text = "🛰 Ты отслеживаешь:\n" + "\n".join(f"- `{row}`" for row in user_tokens)
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

    info["subscribers"].pop(user_id, None)
    if not info["subscribers"]:
        tracked_tokens.pop(address, None)

    pending_threshold_input.pop(user_id, None)

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

                    if not info["subscribers"]:
                        continue

                    symbol = pair["baseToken"]["symbol"]

                    # для каждого подписчика применяем его порог
                    for uid, cfg in list(info["subscribers"].items()):
                        threshold = cfg.get("vol_threshold", 20.0)
                        alerts = check_anomalies(history, threshold)

                        if alerts and now_ts - info["last_alert"] > 5:
                            info["last_alert"] = now_ts
                            msg = (
                                f"🚨 Аномалия объёма (m5) по {symbol}\n"
                                + "\n".join(alerts)
                            )
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
