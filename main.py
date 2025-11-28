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
#     "symbol": str | None,
#     "chain": str | None,
#     "subscribers": {
#         user_id: {
#             "vol_threshold": float | None,
#             "price_threshold": float | None,
#             "mcap_threshold": float | None,
#             "last_price": float | None,
#             "last_volume_m5": float | None,
#             "last_mcap": float | None,
#             "last_ts": float | None,
#             "last_alert_ts": float | None,
#             "volume_history": deque[(ts, buy_vol, sell_vol)],
#         }
#     }
# }

tracked_tokens: dict[str, dict] = {}

# pending_threshold_input[user_id] = {
#     "pending_volume_for": address | None,
#     "pending_price_for": address | None,
#     "pending_mcap_for": address | None,
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
            "last_alert_ts": None,
            "volume_history": deque(maxlen=200),
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


def detect_pump_dump(history: deque) -> str:
    """
    Анализирует историю buy/sell объёмов и определяет возможные памп/дамп.
    Возвращает строку с анализом.
    """
    if len(history) < 3:
        return ""

    recent = list(history)[-5:]  # последние 5 записей
    buy_vols = [b for _, b, _ in recent]
    sell_vols = [s for _, _, s in recent]

    avg_buy = sum(buy_vols) / len(buy_vols) if buy_vols else 0
    avg_sell = sum(sell_vols) / len(sell_vols) if sell_vols else 0

    # Памп: резкое увеличение buy объёма
    if buy_vols and buy_vols[-1] > avg_buy * 2.5:
        return "📈 Возможный памп (высокий buy объём)"
    
    # Дамп: резкое увеличение sell объёма
    if sell_vols and sell_vols[-1] > avg_sell * 2.5:
        return "📉 Возможный дамп (высокий sell объём)"
    
    return ""


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
        "- В алертах есть кнопки, чтобы отключить параметры или всё сразу.\n"
        "- Бот анализирует buy/sell объёмы и показывает возможные памп/дамп."
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

    await update.message.reply_text(text_resp, reply_markup=keyboard)


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

    # ============ МЕНЮ ОТКЛЮЧЁННОГО ТОКЕНА (В СПИСКЕ) ============
    if data.startswith("menu_disabled:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.get(address)

        if not info or user_id not in info.get("subscribers", {}):
            await query.message.reply_text(
                "⚠️ Этот токен больше не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sub = info["subscribers"][user_id]
        symbol = info.get("symbol", "")

        text = (
            f"📌 {symbol} {address}\n\n"
            f"⛔ Отслеживание отключено\n\n"
            f"Выбери параметр для подключения:"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📈 Отслеживать цену", callback_data=f"track_price:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🏦 Отслеживать капу", callback_data=f"track_mcap:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🛰 Отслеживать объём", callback_data=f"track_vol:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🛑 Удалить из списка", callback_data=f"delete:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад", callback_data="back_to_watchlist"
                    ),
                ],
            ]
        )

        await query.edit_message_text(text=text, reply_markup=keyboard)
        return

    # ============ ДЕТАЛЬНОЕ МЕНЮ ТОКЕНА ИЗ WATCHLIST ============
    if data.startswith("menu:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.get(address)

        if not info or user_id not in info.get("subscribers", {}):
            await query.message.reply_text(
                "⚠️ Этот токен больше не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sub = info["subscribers"][user_id]
        label = format_addr_with_meta(address, info)
        symbol = info.get("symbol", "")

        vt = sub.get("vol_threshold")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")

        status_lines = [f"📌 {symbol} {address}"]
        status_lines.append("")
        
        if pt is not None:
            status_lines.append(f"📈 Цена: {pt:.1f}%")
        else:
            status_lines.append("📈 Цена: ⛔")

        if mt is not None:
            status_lines.append(f"🏦 Капа: {mt:.1f}%")
        else:
            status_lines.append("🏦 Капа: ⛔")

        if vt is not None:
            status_lines.append(f"🛰 Объём: {vt:.1f}%")
        else:
            status_lines.append("🛰 Объём: ⛔")

        # Анализ памп/дамп
        pump_dump = detect_pump_dump(sub.get("volume_history", deque()))
        if pump_dump:
            status_lines.append("")
            status_lines.append(pump_dump)

        text = "\n".join(status_lines)

        # Кнопки отключения параметров
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ Цена", callback_data=f"disable_price:{address}"
                    ),
                    InlineKeyboardButton(
                        "❌ Капа", callback_data=f"disable_mcap:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Объём", callback_data=f"disable_vol:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📌 Оставить в списке", callback_data=f"pin:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🛑 Удалить полностью", callback_data=f"delete:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад", callback_data="back_to_watchlist"
                    ),
                ],
            ]
        )

        await query.edit_message_text(text=text, reply_markup=keyboard)
        return

    # ============ ОБНУЛЕНИЕ ПОРОГОВ ============
    if data.startswith("pin:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.get(address)

        if not info or user_id not in info.get("subscribers", {}):
            await query.message.reply_text("⚠️ Токен не найден.")
            return

        sub = info["subscribers"][user_id]
        sub["vol_threshold"] = None
        sub["price_threshold"] = None
        sub["mcap_threshold"] = None

        label = format_addr_with_meta(address, info)
        await query.message.reply_text(
            f"📌 {label} остался в списке, но все пороги сброшены.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ============ УДАЛЕНИЕ ТОКЕНА ============
    if data.startswith("delete:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.get(address)

        if not info or user_id not in info.get("subscribers", {}):
            await query.message.reply_text("⚠️ Токен не найден.")
            return

        label = format_addr_with_meta(address, info)
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

        await query.message.reply_text(
            f"🛑 {label} удален из Watchlist.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ============ НАЗАД В WATCHLIST ============
    if data == "back_to_watchlist":
        await watchlist(update, context)
        return

    # ============ ПОДПИСКА НА ОТСЛЕЖИВАНИЕ ============
    if data.startswith("track_"):
        if data.startswith("track_vol:"):
            address = data.split(":", 1)[1]
            info = tracked_tokens.setdefault(
                address, {"symbol": None, "chain": None, "subscribers": {}}
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
                address, {"symbol": None, "chain": None, "subscribers": {}}
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
                address, {"symbol": None, "chain": None, "subscribers": {}}
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

    # ============ ОТКЛЮЧЕНИЕ ИЗ АЛЕРТА ============
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


# ------------ СПИСОК / ОТКЛЮЧЕНИЕ / УПРАВЛЕНИЕ WATCHLIST ------------

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Интерактивный Watchlist с меню для каждого токена"""
    user_id = update.effective_user.id

    items_active = []
    items_disabled = []
    
    for address, info in tracked_tokens.items():
        sub = info.get("subscribers", {}).get(user_id)
        if not sub:
            continue

        vt = sub.get("vol_threshold")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")

        label = format_addr_with_meta(address, info)
        symbol = label.split()[0]

        # Проверяем, есть ли активные пороги
        has_active = pt is not None or mt is not None or vt is not None

        if has_active:
            parts = []
            if pt is not None:
                parts.append(f"price ≥ {pt:.1f}%")
            if mt is not None:
                parts.append(f"mcap ≥ {mt:.1f}%")
            if vt is not None:
                parts.append(f"vol ≥ {vt:.1f}%")
            
            params = ", ".join(parts)
            btn_text = f"{symbol} • {params}"
            items_active.append((address, btn_text, "menu"))
        else:
            btn_text = f"{symbol} (⛔ отключено)"
            items_disabled.append((address, btn_text, "menu_disabled"))

    if not items_active and not items_disabled:
        await update.message.reply_text(
            "👀 Сейчас ты ничего не отслеживаешь.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Строим кнопки для активных токенов
    keyboard_buttons = []
    
    if items_active:
        keyboard_buttons.append([InlineKeyboardButton("🟢 АКТИВНЫЕ", callback_data="disabled_button")])
        for address, btn_text, callback_prefix in items_active:
            keyboard_buttons.append(
                [InlineKeyboardButton(btn_text, callback_data=f"{callback_prefix}:{address}")]
            )
    
    if items_disabled:
        if items_active:
            keyboard_buttons.append([InlineKeyboardButton("⚫ В СПИСКЕ (БЕЗ АЛЕРТОВ)", callback_data="disabled_button")])
        for address, btn_text, callback_prefix in items_disabled:
            keyboard_buttons.append(
                [InlineKeyboardButton(btn_text, callback_data=f"{callback_prefix}:{address}")]
            )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    text = "🛰 Твой Watchlist:\n\nНажми на токен для управления:"
    await update.message.reply_text(text, reply_markup=keyboard)


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


# ------------ ФОНОВЫЙ МОНИТОР (С АНАЛИЗОМ BUY/SELL) ------------

def analyze_volume_windows(history: deque, current_ts: float) -> dict:
    """
    Анализирует объёмы по временным окнам: 5s, 10s, 20s, 30s
    Возвращает dict с информацией об изменениях.
    """
    windows = {
        "5s": 5,
        "10s": 10,
        "20s": 20,
        "30s": 30,
    }
    
    result = {}
    
    for label, span in windows.items():
        recent = [vol for ts, vol in history if current_ts - ts <= span]
        if len(recent) < 2:
            continue
        
        change = ((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] > 0 else 0
        result[label] = change
    
    return result


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

                    # Попытаемся вытащить buy/sell объёмы (если доступны в API)
                    try:
                        trades = pair.get("trades") or {}
                        buy_vol = float(trades.get("h1Buy", 0) or 0)
                        sell_vol = float(trades.get("h1Sell", 0) or 0)
                    except:
                        buy_vol = vol_m5_cur * 0.5  # приблизительно
                        sell_vol = vol_m5_cur * 0.5

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
                            # Инициализируем историю buy/sell
                            cfg["volume_history"].append((time.time(), buy_vol, sell_vol))
                            continue

                        # Добавляем в историю buy/sell
                        now_ts = time.time()
                        cfg["volume_history"].append((now_ts, buy_vol, sell_vol))

                        price_delta = pct_change(price_cur, cfg["last_price"])
                        vol_delta = pct_change(vol_m5_cur, cfg["last_volume_m5"])
                        mcap_delta = pct_change(mcap_cur, cfg["last_mcap"])

                        pt = cfg.get("price_threshold")
                        vt = cfg.get("vol_threshold")
                        mt = cfg.get("mcap_threshold")

                        triggered = False
                        reason_lines = []

                        # приоритет: цена, капитализация, объём
                        if (
                            pt is not None
                            and price_delta is not None
                            and abs(price_delta) >= pt
                        ):
                            direction = "⬆️" if price_delta > 0 else "⬇️"
                            reason_lines.append(f"{direction} Цена: {price_delta:.2f}%")
                            triggered = True

                        if (
                            not triggered
                            and mt is not None
                            and mcap_delta is not None
                            and abs(mcap_delta) >= mt
                        ):
                            direction = "⬆️" if mcap_delta > 0 else "⬇️"
                            reason_lines.append(
                                f"{direction} Капитализация: {mcap_delta:.2f}%"
                            )
                            triggered = True

                        if (
                            not triggered
                            and vt is not None
                            and vol_delta is not None
                            and abs(vol_delta) >= vt
                        ):
                            direction = "⬆️" if vol_delta > 0 else "⬇️"
                            reason_lines.append(f"{direction} Объём m5: {vol_delta:.2f}%")
                            triggered = True

                        if not triggered:
                            continue

                        # Анализ временных окон
                        vol_windows = analyze_volume_windows(
                            deque([(t, v) for t, _, v in cfg["volume_history"]]), now_ts
                        )

                        # Полная картина изменений
                        extra_lines = []
                        if price_delta is not None:
                            extra_lines.append(f"Цена: {price_delta:+.2f}%")
                        if mcap_delta is not None:
                            extra_lines.append(f"Капитализация: {mcap_delta:+.2f}%")
                        if vol_delta is not None:
                            extra_lines.append(f"Объём m5: {vol_delta:+.2f}%")

                        # Добавляем анализ по окнам
                        for window_label, window_change in vol_windows.items():
                            if window_change != 0:
                                extra_lines.append(f"Объём {window_label}: {window_change:+.1f}%")

                        # Анализ памп/дамп
                        pump_dump = detect_pump_dump(cfg["volume_history"])

                        label = format_addr_with_meta(address, info)

                        # Время с момента последнего алерта
                        last_alert_ts = cfg.get("last_alert_ts") or 0
                        time_since_alert = now_ts - last_alert_ts
                        time_str = f"{int(time_since_alert)}s" if time_since_alert < 60 else f"{int(time_since_alert / 60)}m"

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

                        if pump_dump:
                            msg += f"\n\n⚡ {pump_dump}"

                        msg += f"\n\n⏱️ От последнего сигнала: {time_str}"

                        keyboard = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "❌ Цена",
                                        callback_data=f"disable_price:{address}",
                                    ),
                                    InlineKeyboardButton(
                                        "❌ Капа",
                                        callback_data=f"disable_mcap:{address}",
                                    ),
                                ],
                                [
                                    InlineKeyboardButton(
                                        "❌ Объём",
                                        callback_data=f"disable_vol:{address}",
                                    ),
                                    InlineKeyboardButton(
                                        "🛑 Всё",
                                        callback_data=f"disable_all:{address}",
                                    ),
                                ],
                            ]
                        )

                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=msg,
                                reply_markup=keyboard,
                                parse_mode="Markdown",
                            )

                            logger.info(f"Алёрт отправлен {uid} для {address[:8]}")
                            cfg["last_alert_ts"] = now_ts

                        except Exception as e:
                            logger.error(f"Ошибка отправки алерта {uid}: {e}")

                        # обновляем базовое состояние после алерта
                        cfg["last_price"] = price_cur
                        cfg["last_volume_m5"] = vol_m5_cur
                        cfg["last_mcap"] = mcap_cur
                        cfg["last_ts"] = time.time()

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Критическая ошибка market_watcher: {e}")
            await asyncio.sleep(10)


async def post_init(app: Application):
    logger.info("post_init: запускаем market_watcher в фоне")
    asyncio.create_task(market_watcher(app))


# ------------ MAIN ------------

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден. Проверь переменную окружения.")
        raise SystemExit("BOT_TOKEN is missing")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущен, начинаем polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
