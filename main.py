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
#   "last_checks": deque[(ts, vol_m5, price)],
#   "last_alert": float,
#   "subscribers": { user_id: {"vol_threshold": float|None, "price_threshold": float|None} },
#   "symbol": str | None,
#   "chain": str | None,
# }
tracked_tokens: dict[str, dict] = {}

# user_id -> {"pending_volume_for": address | None, "pending_price_for": address | None}
pending_threshold_input: dict[int, dict] = {}


# ------------ УТИЛИТЫ ------------

def map_chain(chain_id: str | None) -> str:
    """Простое отображение chainId -> человекочитаемое имя сети.[web:93]"""
    if not chain_id:
        return "Unknown"
    mapping = {
        "solana": "Solana",
        "eth": "Ethereum",
        "ethereum": "Ethereum",
        "bsc": "BSC",
        "bnb": "BSC",
        "base": "Base",
        "polygon": "Polygon",
        "arbitrum": "Arbitrum",
        "optimism": "Optimism",
        "avax": "Avalanche",
    }
    return mapping.get(chain_id.lower(), chain_id)


def format_addr_with_meta(address: str, info: dict | None) -> str:
    """Формат для отображения: адрес (тикер, сеть, пороги)."""
    symbol = info.get("symbol") if info else None
    chain = map_chain(info.get("chain")) if info else "Unknown"

    base = address
    meta = []
    if symbol:
        meta.append(symbol)
    if chain:
        meta.append(chain)
    if not meta:
        return base
    return f"{base} ({', '.join(meta)})"


def check_anomalies_generic(
    history: deque[tuple[float, float]],
    user_threshold: float | None,
    label_suffix: str,
):
    """
    history: [(timestamp, value)] — либо volume.m5, либо priceUsd.
    user_threshold: порог в %.
    label_suffix: подпись, например 'volume.m5' или 'price'.
    """
    if len(history) < 2 or user_threshold is None:
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
            alerts.append(f"{direction} {label}: {change:.1f}% ({label_suffix})")

    return alerts


# ------------ КОМАНДЫ ------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я твой крипто-бот!\n\n"
        "1) Отправь адрес токена (Sol/ETH/Base/BNB).\n"
        "2) Нажми кнопку отслеживания объёма или цены.\n"
        "3) Введи порог в %.\n\n"
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


# ------------ ОБРАБОТКА СООБЩЕНИЙ ------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
    }

    # 1) Ввод порога объёма
    if state.get("pending_volume_for"):
        address = state["pending_volume_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи процент для объёма, например: 20"
            )
            return

        info = tracked_tokens.get(address)
        if not info or user_id not in info["subscribers"]:
            await update.message.reply_text(
                "❌ Этот контракт уже не отслеживается. Нажми кнопку ещё раз."
            )
            pending_threshold_input.pop(user_id, None)
            return

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть больше 0. Попробуй ещё раз."
            )
            return

        info["subscribers"][user_id]["vol_threshold"] = threshold
        state["pending_volume_for"] = None
        pending_threshold_input[user_id] = state

        label = format_addr_with_meta(address, info)
        await update.message.reply_text(
            f"✅ Порог объёма для {label}: {threshold:.1f}%.\n"
            f"Алерты по volume.m5 при изменении ≥ этого значения."
        )
        return

    # 2) Ввод порога цены
    if state.get("pending_price_for"):
        address = state["pending_price_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи процент для цены, например: 5"
            )
            return

        info = tracked_tokens.get(address)
        if not info or user_id not in info["subscribers"]:
            await update.message.reply_text(
                "❌ Этот контракт уже не отслеживается. Нажми кнопку ещё раз."
            )
            pending_threshold_input.pop(user_id, None)
            return

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть больше 0. Попробуй ещё раз."
            )
            return

        info["subscribers"][user_id]["price_threshold"] = threshold
        state["pending_price_for"] = None
        pending_threshold_input[user_id] = state

        label = format_addr_with_meta(address, info)
        await update.message.reply_text(
            f"✅ Порог цены для {label}: {threshold:.1f}%.\n"
            f"Алерты по priceUsd при изменении ≥ этого значения."
        )
        return

    # 3) Обычный режим: считаем, что это контракт
    address = text
    await update.message.reply_text(f"🔍 Анализирую {address[:12]}...")

    async with aiohttp.ClientSession() as session:
        raw = await get_token_pairs_by_address(session, address)

    pair = pick_best_pair(raw)

    if pair:
        price = float(pair.get("priceUsd", 0) or 0)

        volume_info = pair.get("volume") or {}
        volume_24h = volume_info.get("h24", 0) or 0
        volume_m5 = volume_info.get("m5", 0) or 0

        mcap = pair.get("marketCap") or pair.get("mcap") or 0
        fdv = pair.get("fdv") or 0
        if not mcap and fdv:
            mcap = fdv

        symbol = pair["baseToken"]["symbol"]
        chain_id = pair.get("chainId")  # есть в ответе DexScreener[web:93]
        chain_name = map_chain(chain_id)

        # сохраняем метаданные, если ещё не были
        info = tracked_tokens.get(address)
        if not info:
            info = {
                "last_checks": deque(maxlen=500),
                "last_alert": 0.0,
                "subscribers": {},
                "symbol": symbol,
                "chain": chain_id,
            }
            tracked_tokens[address] = info
        else:
            info.setdefault("symbol", symbol)
            info.setdefault("chain", chain_id)

        text_resp = (
            f"💎 {symbol} ({chain_name})\n"
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
                        "🛰 Следить за объёмом (m5)",
                        callback_data=f"track_vol:{address}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📈 Следить за ценой",
                        callback_data=f"track_price:{address}",
                    )
                ],
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
    user_id = query.from_user.id

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
    }

    if data.startswith("track_vol:"):
        address = data.split(":", 1)[1]

        info = tracked_tokens.get(address)
        if not info:
            info = {
                "last_checks": deque(maxlen=500),
                "last_alert": 0.0,
                "subscribers": {},
                "symbol": None,
                "chain": None,
            }
            tracked_tokens[address] = info

        info["subscribers"].setdefault(
            user_id, {"vol_threshold": None, "price_threshold": None}
        )

        state["pending_volume_for"] = address
        pending_threshold_input[user_id] = state

        await query.edit_message_reply_markup(reply_markup=None)
        label = format_addr_with_meta(address, info)
        await query.message.reply_text(
            f"📊 Введи процент изменения объёма m5 для {label}, при котором слать алерт.\n"
            f"Например: 20"
        )

    elif data.startswith("track_price:"):
        address = data.split(":", 1)[1]

        info = tracked_tokens.get(address)
        if not info:
            info = {
                "last_checks": deque(maxlen=500),
                "last_alert": 0.0,
                "subscribers": {},
                "symbol": None,
                "chain": None,
            }
            tracked_tokens[address] = info

        info["subscribers"].setdefault(
            user_id, {"vol_threshold": None, "price_threshold": None}
        )

        state["pending_price_for"] = address
        pending_threshold_input[user_id] = state

        await query.edit_message_reply_markup(reply_markup=None)
        label = format_addr_with_meta(address, info)
        await query.message.reply_text(
            f"📈 Введи процент изменения цены для {label}, при котором слать алерт.\n"
            f"Например: 5"
        )


# ------------ СПИСОК / ОТКЛЮЧЕНИЕ ------------

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    rows = []
    for address, info in tracked_tokens.items():
        cfg = info["subscribers"].get(user_id)
        if not cfg:
            continue
        vt = cfg.get("vol_threshold")
        pt = cfg.get("price_threshold")
        if vt is None and pt is None:
            continue

        parts = []
        if vt is not None:
            parts.append(f"vol ≥ {vt:.1f}%")
        if pt is not None:
            parts.append(f"price ≥ {pt:.1f}%")

        label = format_addr_with_meta(address, info)
        rows.append(f"{label} ({', '.join(parts)})")

    if not rows:
        await update.message.reply_text("👀 Сейчас ты ничего не отслеживаешь.")
        return

    text = "🛰 Ты отслеживаешь:\n" + "\n".join(f"- {row}" for row in rows)
    await update.message.reply_text(text)


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

    state = pending_threshold_input.get(user_id)
    if state:
        if state.get("pending_volume_for") == address:
            state["pending_volume_for"] = None
        if state.get("pending_price_for") == address:
            state["pending_price_for"] = None
        pending_threshold_input[user_id] = state

    label = format_addr_with_meta(address, info or {})
    await update.message.reply_text(f"✅ Отключил отслеживание для {label}.")


# ------------ ФОНОВЫЙ МОНИТОР ------------

async def market_watcher(app: Application):
    while True:
        logger.info("MARKET_WATCHER_TICK")
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
                    price = float(pair.get("priceUsd", 0) or 0)

                    now_ts = time.time()
                    history_full: deque = info["last_checks"]
                    history_full.append((now_ts, volume_m5, price))

                    if not info["subscribers"]:
                        continue

                    symbol = info.get("symbol") or pair["baseToken"]["symbol"]

                    hist_vol = deque(
                        [(ts, v) for (ts, v, p) in history_full], maxlen=history_full.maxlen
                    )
                    hist_price = deque(
                        [(ts, p) for (ts, v, p) in history_full], maxlen=history_full.maxlen
                    )

                    for uid, cfg in list(info["subscribers"].items()):
                        vt = cfg.get("vol_threshold")
                        pt = cfg.get("price_threshold")

                        vol_alerts = (
                            check_anomalies_generic(hist_vol, vt, "volume.m5")
                            if vt is not None
                            else []
                        )
                        price_alerts = (
                            check_anomalies_generic(hist_price, pt, "price")
                            if pt is not None
                            else []
                        )

                        if (vol_alerts or price_alerts) and time.time() - info["last_alert"] > 5:
                            info["last_alert"] = time.time()
                            parts = []
                            if vol_alerts:
                                parts.append("🚨 Объём:\n" + "\n".join(vol_alerts))
                            if price_alerts:
                                parts.append("⚡ Цена:\n" + "\n".join(price_alerts))

                            label = format_addr_with_meta(address, info)
                            msg = f"{symbol}\n{label}\n\n" + "\n\n".join(parts)
                            try:
                                await app.bot.send_message(chat_id=uid, text=msg)
                            except Exception as e:
                                logger.warning(f"Send alert error: {e}")
                except Exception as e:
                    logger.warning(f"Market watcher error for {address}: {e}")

        await asyncio.sleep(5)


async def post_init(app: Application):
    app.create_task(market_watcher(app))
    logger.info("🚀 Market watcher запущен…")


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
