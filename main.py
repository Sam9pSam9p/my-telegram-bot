import os
import time
import logging
import asyncio

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
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

# tracked_tokens[address] = {
#   "symbol": str | None,
#   "chain": str | None,
#   "subscribers": {
#       user_id: {
#           "vol_threshold": float | None,
#           "price_threshold": float | None,
#           "mcap_threshold": float | None,
#           "last_price": float | None,
#           "last_volume_m5": float | None,
#           "last_mcap": float | None,
#           "last_ts": float | None,
#       }
#   }
# }
tracked_tokens: dict[str, dict] = {}

# pending_threshold_input[user_id] = {
#   "pending_volume_for": address | None,
#   "pending_price_for": address | None,
#   "pending_mcap_for": address | None,
# }
pending_threshold_input: dict[int, dict] = {}


# ------------ УТИЛИТЫ ------------

def map_chain(chain_id: str | None) -> str:
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


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def ensure_subscriber(info: dict, user_id: int) -> dict:
    subs = info.setdefault("subscribers", {})
    sub = subs.get(user_id)
    if not sub:
        sub = {
            "vol_threshold": None,
            "price_threshold": None,
            "mcap_threshold": None,
            "last_price": None,
            "last_volume_m5": None,
            "last_mcap": None,
            "last_ts": None,
        }
        subs[user_id] = sub
    return sub


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Отслеживать токен")],
            [KeyboardButton("📋 Watchlist"), KeyboardButton("❓ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# ------------ КОМАНДЫ ------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start от {update.effective_user.id}")
    await update.message.reply_text(
        "🤖 Привет! Я крипто-бот.\n\n"
        "1) Отправь адрес токена (Sol/ETH/Base/BNB).\n"
        "2) Нажми кнопку отслеживания цены / объёма / капы.\n"
        "3) Введи порог в %.\n\n"
        "/watchlist — текущие подписки\n"
        "/unwatch <адрес> — убрать токен\n"
        "/price — цена BTC\n\n"
        "Или используй кнопки меню внизу экрана.",
        reply_markup=main_menu_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Краткая справка:\n"
        "- Отправь адрес контракта, чтобы получить инфо и кнопки отслеживания.\n"
        "- Выбери, что отслеживать (цена, капа, объём) и задай порог в %.\n"
        "- /watchlist покажет все активные токены.\n"
        "- В алертах есть кнопки, чтобы отключить параметры или всё сразу."
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/price от {update.effective_user.id}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd"
            ) as resp:
                data = await resp.json()
        btc_price = data["bitcoin"]["usd"]
        await update.message.reply_text(
            f"₿ Bitcoin: ${btc_price:,}", reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка /price: {e}")
        await update.message.reply_text("❌ Ошибка получения цены BTC")


# ------------ ОБРАБОТКА СООБЩЕНИЙ ------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    logger.info(f"MSG от {user_id}: {text[:80]}")

    # Обработка кнопок ReplyKeyboard
    if text == "📋 Watchlist":
        await watchlist(update, context)
        return
    if text == "❓ Помощь":
        await help_cmd(update, context)
        return
    if text == "➕ Отслеживать токен":
        await update.message.reply_text(
            "Отправь адрес контракта токена, который хочешь отслеживать.",
            reply_markup=main_menu_keyboard(),
        )
        return

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
    }

    # Ввод порога объёма
    if state.get("pending_volume_for"):
        address = state["pending_volume_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи %, например: 20",
                reply_markup=main_menu_keyboard(),
            )
            return

        info = tracked_tokens.get(address)
        if not info:
            await update.message.reply_text(
                "❌ Этот контракт уже не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            pending_threshold_input.pop(user_id, None)
            return

        sub = ensure_subscriber(info, user_id)

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть > 0.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sub["vol_threshold"] = threshold
        state["pending_volume_for"] = None
        pending_threshold_input[user_id] = state

        label = format_addr_with_meta(address, info)
        await update.message.reply_text(
            f"✅ Порог объёма для {label}: {threshold:.1f}%.\n"
            f"Бот будет слать сигнал при изменении m5 volume ≥ этого порога.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Ввод порога цены
    if state.get("pending_price_for"):
        address = state["pending_price_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи %, например: 5",
                reply_markup=main_menu_keyboard(),
            )
            return

        info = tracked_tokens.get(address)
        if not info:
            await update.message.reply_text(
                "❌ Этот контракт уже не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            pending_threshold_input.pop(user_id, None)
            return

        sub = ensure_subscriber(info, user_id)

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть > 0.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sub["price_threshold"] = threshold
        state["pending_price_for"] = None
        pending_threshold_input[user_id] = state

        label = format_addr_with_meta(address, info)
        await update.message.reply_text(
            f"✅ Порог цены для {label}: {threshold:.1f}%.\n"
            f"Приоритетный сигнал: изменение цены относительно предыдущего состояния.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Ввод порога капитализации
    if state.get("pending_mcap_for"):
        address = state["pending_mcap_for"]
        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи %, например: 10",
                reply_markup=main_menu_keyboard(),
            )
            return

        info = tracked_tokens.get(address)
        if not info:
            await update.message.reply_text(
                "❌ Этот контракт уже не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            pending_threshold_input.pop(user_id, None)
            return

        sub = ensure_subscriber(info, user_id)

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть > 0.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sub["mcap_threshold"] = threshold
        state["pending_mcap_for"] = None
        pending_threshold_input[user_id] = state

        label = format_addr_with_meta(address, info)
        await update.message.reply_text(
            f"✅ Порог капитализации для {label}: {threshold:.1f}%.\n"
            f"Приоритетный сигнал: изменение капитализации относительно предыдущего состояния.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Если это не ввод порога — считаем, что адрес контракта
    address = text
    await update.message.reply_text(
        f"🔍 Анализирую {address[:12]}...", reply_markup=main_menu_keyboard()
    )

    try:
        async with aiohttp.ClientSession() as session:
            raw = await get_token_pairs_by_address(session, address)
        pair = pick_best_pair(raw)
    except Exception as e:
        logger.error(f"Ошибка запроса токена {address}: {e}")
        await update.message.reply_text(
            "❌ Ошибка запроса токена.", reply_markup=main_menu_keyboard()
        )
        return

    if not pair:
        await update.message.reply_text(
            "❌ Токен не найден. Проверь адрес!",
            reply_markup=main_menu_keyboard(),
        )
        return

    price_cur = float(pair.get("priceUsd", 0) or 0)
    volume_info = pair.get("volume") or {}
    vol_m5_cur = float(volume_info.get("m5", 0) or 0)
    vol_24h_cur = float(volume_info.get("h24", 0) or 0)
    mcap_cur = float(pair.get("marketCap") or pair.get("mcap") or 0)
    fdv = float(pair.get("fdv") or 0)
    if not mcap_cur and fdv:
        mcap_cur = fdv

    symbol = pair["baseToken"]["symbol"]
    chain_id = pair.get("chainId")
    chain_name = map_chain(chain_id)

    info = tracked_tokens.get(address)
    if not info:
        info = {
            "symbol": symbol,
            "chain": chain_id,
            "subscribers": {},
        }
        tracked_tokens[address] = info
    else:
        info.setdefault("symbol", symbol)
        info.setdefault("chain", chain_id)
        info.setdefault("subscribers", {})

    text_resp = (
        f"💎 {symbol} ({chain_name})\n"
        f"💰 Цена: ${price_cur:,.6f}\n"
        f"🕒 Объём 5m: ${vol_m5_cur:,.0f}\n"
        f"📊 Объём 24ч: ${vol_24h_cur:,.0f}\n"
        f"🏦 Капитализация: ${mcap_cur:,.0f}\n"
        f"🔗 {pair['url']}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📈 Следить за ценой", callback_data=f"track_price:{address}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏦 Следить за капой", callback_data=f"track_mcap:{address}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🛰 Следить за объёмом (m5)", callback_data=f"track_vol:{address}"
                ),
            ],
        ]
    )

    await update.message.reply_text(
        text_resp, reply_markup=keyboard,
    )


# ------------ КНОПКИ ------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id
    logger.info(f"BTN от {user_id}: {data}")

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
    }

    # Подписка
    if data.startswith("track_"):
        if data.startswith("track_vol:"):
            address = data.split(":", 1)[1]
            info = tracked_tokens.setdefault(
                address,
                {"symbol": None, "chain": None, "subscribers": {}},
            )
            ensure_subscriber(info, user_id)
            state["pending_volume_for"] = address
            pending_threshold_input[user_id] = state
            await query.edit_message_reply_markup(reply_markup=None)
            label = format_addr_with_meta(address, info)
            await query.message.reply_text(
                f"🛰 Введи порог изменения объёма m5 в % для {label}.\n"
                f"Например: 20",
                reply_markup=main_menu_keyboard(),
            )
            return

        if data.startswith("track_price:"):
            address = data.split(":", 1)[1]
            info = tracked_tokens.setdefault(
                address,
                {"symbol": None, "chain": None, "subscribers": {}},
            )
            ensure_subscriber(info, user_id)
            state["pending_price_for"] = address
            pending_threshold_input[user_id] = state
            await query.edit_message_reply_markup(reply_markup=None)
            label = format_addr_with_meta(address, info)
            await query.message.reply_text(
                f"📈 Введи порог изменения цены в % для {label}.\n"
                f"Например: 5",
                reply_markup=main_menu_keyboard(),
            )
            return

        if data.startswith("track_mcap:"):
            address = data.split(":", 1)[1]
            info = tracked_tokens.setdefault(
                address,
                {"symbol": None, "chain": None, "subscribers": {}},
            )
            ensure_subscriber(info, user_id)
            state["pending_mcap_for"] = address
            pending_threshold_input[user_id] = state
            await query.edit_message_reply_markup(reply_markup=None)
            label = format_addr_with_meta(address, info)
            await query.message.reply_text(
                f"🏦 Введи порог изменения капитализации в % для {label}.\n"
                f"Например: 10",
                reply_markup=main_menu_keyboard(),
            )
            return

    # Отключение из алерта
    if data.startswith("disable_"):
        prefix, address = data.split(":", 1)
        kind = prefix.replace("disable_", "")
        info = tracked_tokens.get(address)
        if not info:
            await query.message.reply_text(
                "⚠️ Этот токен уже не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            return

        subs = info.get("subscribers", {})
        sub = subs.get(user_id)
        if not sub:
            await query.message.reply_text(
                "⚠️ Подписка для этого токена уже снята.",
                reply_markup=main_menu_keyboard(),
            )
            return

        label = format_addr_with_meta(address, info)

        if kind == "price":
            sub["price_threshold"] = None
            await query.message.reply_text(
                f"✅ Отключены алерты цены для {label}.",
                reply_markup=main_menu_keyboard(),
            )
        elif kind == "mcap":
            sub["mcap_threshold"] = None
            await query.message.reply_text(
                f"✅ Отключены алерты капы для {label}.",
                reply_markup=main_menu_keyboard(),
            )
        elif kind == "vol":
            sub["vol_threshold"] = None
            await query.message.reply_text(
                f"✅ Отключены алерты объёма для {label}.",
                reply_markup=main_menu_keyboard(),
            )
        elif kind == "all":
            subs.pop(user_id, None)
            if not subs:
                tracked_tokens.pop(address, None)
            await query.message.reply_text(
                f"🛑 Полностью отключено отслеживание {label}.",
                reply_markup=main_menu_keyboard(),
            )

        return


# ------------ СПИСОК / ОТКЛЮЧЕНИЕ ------------

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = []
    for address, info in tracked_tokens.items():
        sub = info.get("subscribers", {}).get(user_id)
        if not sub:
            continue
        vt = sub.get("vol_threshold")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")
        parts = []
        if pt is not None:
            parts.append(f"price ≥ {pt:.1f}%")
        if mt is not None:
            parts.append(f"mcap ≥ {mt:.1f}%")
        if vt is not None:
            parts.append(f"vol ≥ {vt:.1f}%")
        if not parts:
            parts.append("параметры отключены")
        label = format_addr_with_meta(address, info)
        rows.append(f"{label} ({', '.join(parts)})")

    if not rows:
        await update.message.reply_text(
            "👀 Сейчас ты ничего не отслеживаешь.",
            reply_markup=main_menu_keyboard(),
        )
        return

    text = "🛰 Ты отслеживаешь:\n" + "\n".join(f"- {row}" for row in rows)
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Используй: /unwatch <адрес_контракта>",
            reply_markup=main_menu_keyboard(),
        )
        return
    address = context.args[0].strip()
    info = tracked_tokens.get(address)
    if not info or user_id not in info.get("subscribers", {}):
        await update.message.reply_text(
            "❌ Этот адрес ты сейчас не отслеживаешь.",
            reply_markup=main_menu_keyboard(),
        )
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
        if state.get("pending_mcap_for") == address:
            state["pending_mcap_for"] = None
        pending_threshold_input[user_id] = state
    label = format_addr_with_meta(address, info or {})
    await update.message.reply_text(
        f"✅ Отключил отслеживание для {label}.",
        reply_markup=main_menu_keyboard(),
    )


# ------------ ФОНОВЫЙ МОНИТОР ------------

async def market_watcher(app: Application):
    logger.info("🚀 Market watcher запущен")
    while True:
        try:
            if not tracked_tokens:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession() as session:
                for address, info in list(tracked_tokens.items()):
                    subs = info.get("subscribers") or {}
                    if not subs:
                        continue

                    try:
                        raw = await get_token_pairs_by_address(session, address)
                        pair = pick_best_pair(raw)
                    except Exception as e:
                        logger.error(f"Ошибка обновления токена {address[:8]}: {e}")
                        continue

                    if not pair:
                        logger.warning(f"Нет пары для {address}")
                        continue

                    price_cur = float(pair.get("priceUsd", 0) or 0)
                    volume_info = pair.get("volume") or {}
                    vol_m5_cur = float(volume_info.get("m5", 0) or 0)
                    mcap_cur = float(pair.get("marketCap") or pair.get("mcap") or 0)
                    fdv = float(pair.get("fdv") or 0)
                    if not mcap_cur and fdv:
                        mcap_cur = fdv

                    symbol = info.get("symbol") or pair["baseToken"]["symbol"]
                    info["symbol"] = symbol
                    info.setdefault("chain", pair.get("chainId"))

                    for uid, cfg in list(subs.items()):
                        # зафиксировать базовое состояние, если его нет
                        if cfg.get("last_price") is None:
                            cfg["last_price"] = price_cur
                            cfg["last_volume_m5"] = vol_m5_cur
                            cfg["last_mcap"] = mcap_cur
                            cfg["last_ts"] = time.time()
                            continue

                        price_delta = pct_change(price_cur, cfg["last_price"])
                        vol_delta = pct_change(vol_m5_cur, cfg["last_volume_m5"])
                        mcap_delta = pct_change(mcap_cur, cfg["last_mcap"])

                        pt = cfg.get("price_threshold")
                        vt = cfg.get("vol_threshold")
                        mt = cfg.get("mcap_threshold")

                        triggered = False
                        reason_lines = []

                        # приоритет: цена, капитализация, объём
                        if pt is not None and price_delta is not None and abs(price_delta) >= pt:
                            direction = "⬆️" if price_delta > 0 else "⬇️"
                            reason_lines.append(f"{direction} Цена: {price_delta:.2f}%")
                            triggered = True

                        if not triggered and mt is not None and mcap_delta is not None and abs(mcap_delta) >= mt:
                            direction = "⬆️" if mcap_delta > 0 else "⬇️"
                            reason_lines.append(f"{direction} Капитализация: {mcap_delta:.2f}%")
                            triggered = True

                        if not triggered and vt is not None and vol_delta is not None and abs(vol_delta) >= vt:
                            direction = "⬆️" if vol_delta > 0 else "⬇️"
                            reason_lines.append(f"{direction} Объём m5: {vol_delta:.2f}%")
                            triggered = True

                        if not triggered:
                            continue

                        # Полная картина изменений
                        extra_lines = []
                        if price_delta is not None:
                            extra_lines.append(f"Цена: {price_delta:+.2f}%")
                        if mcap_delta is not None:
                            extra_lines.append(f"Капитализация: {mcap_delta:+.2f}%")
                        if vol_delta is not None:
                            extra_lines.append(f"Объём m5: {vol_delta:+.2f}%")

                        label = format_addr_with_meta(address, info)
                        msg = (
                            f"🚨 {symbol}\n{label}\n\n"
                            f"{'; '.join(reason_lines)}\n\n"
                            f"Текущие значения:\n"
                            f"💰 Цена: ${price_cur:,.6f}\n"
                            f"🕒 Объём 5m: ${vol_m5_cur:,.0f}\n"
                            f"🏦 Капитализация: ${mcap_cur:,.0f}\n\n"
                            f"Изменение от предыдущего состояния:\n"
                            f"{'; '.join(extra_lines)}"
                        )

                        keyboard = InlineKeyboardMarkup(
