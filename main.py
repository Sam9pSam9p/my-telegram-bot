import os
import time
import logging
import asyncio
import json
import re
from collections import deque
from dotenv import load_dotenv
from typing import Dict, List, Optional

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

# ============ КОНСТАНТЫ И НАСТРОЙКИ ============

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
BASESCAN_API_KEY = os.getenv("BASESCAN_API_KEY", "")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")

# Лимиты системы
MAX_TOKENS_PER_USER = 50
MAX_WALLETS_PER_USER = 10
API_RATE_LIMIT_DELAY = 1  # секунды между запросами

# Файлы данных
WALLETS_DATA_FILE = "bot_wallets.json"
TOKENS_DATA_FILE = "bot_tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============ МЕНЕДЖЕРЫ ДАННЫХ ============

class DataManager:
    """Управление сохранением и загрузкой данных"""
    
    @staticmethod
    def load_wallets() -> Dict[int, dict]:
        """Загружает данные кошельков"""
        try:
            with open(WALLETS_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                logger.info(f"📊 Кошельки загружены: {len(data)} пользователей")
                return {int(k): v for k, v in data.items()}
        except FileNotFoundError:
            logger.info("📊 Новое хранилище кошельков создано")
            return {}

    @staticmethod
    def save_wallets(data: Dict[int, dict]):
        """Сохраняет данные кошельков"""
        try:
            with open(WALLETS_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения кошельков: {e}")

    @staticmethod
    def load_tokens() -> Dict[str, dict]:
        """Загружает данные отслеживаемых токенов"""
        try:
            with open(TOKENS_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                logger.info(f"📊 Токены загружены: {len(data)} записей")
                return data
        except FileNotFoundError:
            logger.info("📊 Новое хранилище токенов создано")
            return {}

    @staticmethod
    def save_tokens(data: Dict[str, dict]):
        """Сохраняет данные токенов"""
        try:
            with open(TOKENS_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения токенов: {e}")

# ============ ВАЛИДАТОРЫ ============

class AddressValidator:
    """Валидация блокчейн-адресов"""
    
    @staticmethod
    def validate_evm_address(address: str) -> bool:
        """Проверяет валидность EVM адреса"""
        pattern = r'^0x[a-fA-F0-9]{40}$'
        return bool(re.match(pattern, address))
    
    @staticmethod
    def validate_solana_address(address: str) -> bool:
        """Проверяет валидность Solana адреса"""
        pattern = r'^[1-9A-HJ-NP-Za-km-z]{32,44}$'
        return bool(re.match(pattern, address))
    
    @staticmethod
    def validate_address(address: str, chain: str = "auto") -> bool:
        """Универсальная валидация адреса"""
        if chain == "solana" or (chain == "auto" and not address.startswith("0x")):
            return AddressValidator.validate_solana_address(address)
        else:
            return AddressValidator.validate_evm_address(address)

class RateLimiter:
    """Система ограничения запросов"""
    
    def __init__(self):
        self.user_requests: Dict[int, List[float]] = {}
        self.global_requests: List[float] = []
    
    async def check_user_limit(self, user_id: int, max_requests: int = 10, window: int = 60) -> bool:
        """Проверяет лимит запросов пользователя"""
        now = time.time()
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []
        
        # Удаляем старые запросы
        self.user_requests[user_id] = [
            req_time for req_time in self.user_requests[user_id] 
            if now - req_time < window
        ]
        
        if len(self.user_requests[user_id]) >= max_requests:
            return False
        
        self.user_requests[user_id].append(now)
        return True
    
    async def wait_if_needed(self):
        """Ждет если нужно соблюсти глобальный rate limit"""
        now = time.time()
        self.global_requests = [
            req_time for req_time in self.global_requests 
            if now - req_time < 60
        ]
        
        if len(self.global_requests) >= 30:  # 30 запросов в минуту
            await asyncio.sleep(1)
        
        self.global_requests.append(now)

# ============ GLOBALS ============

# Инициализация менеджеров данных
user_wallets = DataManager.load_wallets()
tracked_tokens = DataManager.load_tokens()

# Системы ограничений
rate_limiter = RateLimiter()

# Временные состояния
pending_threshold_input: Dict[int, dict] = {}
pending_wallet_input: Dict[int, dict] = {}

# ============ ОСНОВНЫЕ КЛАССЫ ============

class WalletManager:
    """Управление кошельками пользователей"""
    
    @staticmethod
    def get_user_wallets(user_id: int) -> dict:
        """Получает кошельки пользователя"""
        if user_id not in user_wallets:
            user_wallets[user_id] = {"wallets": {}, "last_update": 0}
            DataManager.save_wallets(user_wallets)
        return user_wallets[user_id]
    
    @staticmethod
    def can_add_wallet(user_id: int) -> bool:
        """Проверяет может ли пользователь добавить кошелек"""
        user_data = WalletManager.get_user_wallets(user_id)
        return len(user_data.get("wallets", {})) < MAX_WALLETS_PER_USER
    
    @staticmethod
    def save_wallet(user_id: int, wallet_id: str, wallet_data: dict):
        """Сохраняет кошелек"""
        user_data = WalletManager.get_user_wallets(user_id)
        user_data["wallets"][wallet_id] = wallet_data
        DataManager.save_wallets(user_wallets)

class TokenManager:
    """Управление отслеживаемыми токенами"""
    
    @staticmethod
    def can_add_token(user_id: int) -> bool:
        """Проверяет может ли пользователь добавить токен"""
        user_token_count = 0
        for token_data in tracked_tokens.values():
            if user_id in token_data.get("subscribers", {}):
                user_token_count += 1
        return user_token_count < MAX_TOKENS_PER_USER
    
    @staticmethod
    def get_user_tokens_count(user_id: int) -> int:
        """Возвращает количество токенов пользователя"""
        count = 0
        for token_data in tracked_tokens.values():
            if user_id in token_data.get("subscribers", {}):
                count += 1
        return count
    
    @staticmethod
    def save_tokens():
        """Сохраняет токены"""
        DataManager.save_tokens(tracked_tokens)

# ============ ОБНОВЛЕННЫЕ ФУНКЦИИ ============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сообщений с проверкой лимитов"""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    
    # Проверка rate limit
    if not await rate_limiter.check_user_limit(user_id):
        await update.message.reply_text(
            "⚠️ Слишком много запросов. Подожди минуту.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    logger.info(f"MSG от {user_id}: {text[:80]}")
    
    # Обработка кнопок главного меню (остается без изменений)
    if text == "📋 Watchlist":
        await watchlist(update, context)
        return
    
    if text == "💼 Мой портфель":
        await show_portfolio_menu(update, context)
        return
    
    # ... остальные обработчики кнопок
    
    if text == "➕ Добавить токен":
        if not TokenManager.can_add_token(user_id):
            await update.message.reply_text(
                f"❌ Достигнут лимит токенов ({MAX_TOKENS_PER_USER}). "
                f"Удали некоторые токены чтобы добавить новые.",
                reply_markup=main_menu_keyboard(),
            )
            return
        
        await update.message.reply_text(
            "📍 Отправь адрес контракта токена, который хочешь отслеживать.\n\n"
            "Примеры:\n"
            "• Solana: EPjFWdd5VqgQfm6ErMqPRyrEGSs2xKXWbdcZ3dWoE8Z\n"
            "• Ethereum: 0xdAC17F958D2ee523a2206206994597C13D831ec7 (USDT)\n"
            "• Base: 0x833589fCD6eDb6E08f4c7C32D4f71b1566dA3633 (USDC)",
            reply_markup=main_menu_keyboard(),
        )
        return
    
    # Обработка добавления кошелька
    if user_id in pending_wallet_input:
        state = pending_wallet_input[user_id]
        
        if text == "Отмена":
            pending_wallet_input.pop(user_id, None)
            await update.message.reply_text("❌ Отмена", reply_markup=main_menu_keyboard())
            return
        
        if state.get("step") == "address":
            # Валидация адреса
            if not AddressValidator.validate_address(text):
                await update.message.reply_text(
                    "❌ Неверный формат адреса. Проверь и отправь снова.\n\n"
                    "EVM адреса должны начинаться с 0x и иметь 42 символа\n"
                    "Solana адреса должны быть 32-44 символа",
                    reply_markup=main_menu_keyboard()
                )
                return
            
            # Проверка лимита кошельков
            if not WalletManager.can_add_wallet(user_id):
                await update.message.reply_text(
                    f"❌ Достигнут лимит кошельков ({MAX_WALLETS_PER_USER}). "
                    f"Удали некоторые кошельки чтобы добавить новые.",
                    reply_markup=main_menu_keyboard()
                )
                pending_wallet_input.pop(user_id, None)
                return
            
            state["address"] = text
            state["step"] = "chain"
            
            keyboard = ReplyKeyboardMarkup(
                [
                    [KeyboardButton("Solana"), KeyboardButton("Ethereum")],
                    [KeyboardButton("Base"), KeyboardButton("BSC")],
                    [KeyboardButton("Отмена")]
                ],
                resize_keyboard=True,
                one_time_keyboard=True
            )
            
            await update.message.reply_text(
                "🌐 Выбери сеть кошелька:",
                reply_markup=keyboard
            )
            return
        
        # ... остальная логика добавления кошелька
    
    # Обработка добавления токена
    if len(text) > 20:  # Предположительно адрес токена
        # Проверка лимита токенов
        if not TokenManager.can_add_token(user_id):
            await update.message.reply_text(
                f"❌ Достигнут лимит токенов ({MAX_TOKENS_PER_USER}). "
                f"Удали некоторые токены чтобы добавить новые.",
                reply_markup=main_menu_keyboard(),
            )
            return
        
        # Валидация адреса токена
        if not AddressValidator.validate_address(text):
            await update.message.reply_text(
                "❌ Неверный формат адреса токена. Проверь адрес и попробуй снова.",
                reply_markup=main_menu_keyboard()
            )
            return
        
        await process_token_address(update, context, text)
        return

async def process_token_address(update: Update, context: ContextTypes.DEFAULT_TYPE, address: str):
    """Обрабатывает добавление токена с валидацией"""
    user_id = update.effective_user.id
    
    await update.message.reply_text(
        f"🔍 Анализирую {address[:12]}...", 
        reply_markup=main_menu_keyboard()
    )

    try:
        # Rate limit для внешних API
        await rate_limiter.wait_if_needed()
        
        async with aiohttp.ClientSession() as session:
            raw = await get_token_pairs_by_address(session, address)
            pair = pick_best_pair(raw)

    except Exception as e:
        logger.error(f"Ошибка запроса токена {address}: {e}")
        await update.message.reply_text(
            "❌ Ошибка запроса токена.", 
            reply_markup=main_menu_keyboard()
        )
        return

    if not pair:
        await update.message.reply_text(
            "❌ Токен не найден. Проверь адрес!",
            reply_markup=main_menu_keyboard(),
        )
        return
    
    # Сохраняем токен
    price_cur = float(pair.get("priceUsd", 0) or 0)
    symbol = pair["baseToken"]["symbol"]
    chain_id = pair.get("chainId")
    
    info = tracked_tokens.get(address)
    if not info:
        info = {
            "symbol": symbol,
            "chain": chain_id,
            "subscribers": {},
        }
        tracked_tokens[address] = info
    else:
        info["symbol"] = symbol
        info["chain"] = chain_id
    
    # Сохраняем изменения
    TokenManager.save_tokens()
    
    # Показываем интерфейс настройки (существующий код)
    # ...

# ============ ОБНОВЛЕННАЯ ФУНКЦИЯ WATCHLIST ============

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр Watchlist с информацией о лимитах"""
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

        symbol = info.get("symbol", "")
        short_address = short_addr(address)

        has_active = pt is not None or mt is not None or vt is not None

        if has_active:
            parts = []
            if pt is not None:
                parts.append(f"📈 {pt:.1f}%")
            if mt is not None:
                parts.append(f"🏦 {mt:.1f}%")
            if vt is not None:
                parts.append(f"🛰 {vt:.1f}%")
            
            params = " ".join(parts)
            btn_text = f"{symbol} {short_address} {params}"
            items_active.append((address, btn_text, "menu"))
        else:
            btn_text = f"{symbol} {short_address} ⛔"
            items_disabled.append((address, btn_text, "menu_disabled"))

    current_count = len(items_active) + len(items_disabled)
    
    if not items_active and not items_disabled:
        text = (
            "👀 Сейчас ты ничего не отслеживаешь.\n\n"
            f"📊 Лимит: {MAX_TOKENS_PER_USER} токенов"
        )
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
        return

    keyboard_buttons = []
    
    if items_active:
        keyboard_buttons.append([InlineKeyboardButton("🟢 АКТИВНЫЕ", callback_data="noop")])
        for address, btn_text, callback_prefix in items_active:
            keyboard_buttons.append(
                [InlineKeyboardButton(btn_text, callback_data=f"{callback_prefix}:{address}")]
            )
    
    if items_disabled:
        if items_active:
            keyboard_buttons.append([InlineKeyboardButton("⚫ БЕЗ АЛЕРТОВ", callback_data="noop")])
        for address, btn_text, callback_prefix in items_disabled:
            keyboard_buttons.append(
                [InlineKeyboardButton(btn_text, callback_data=f"{callback_prefix}:{address}")]
            )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    text = (
        f"🛰 **Твой Watchlist:** {current_count}/{MAX_TOKENS_PER_USER}\n\n"
        "Нажми на токен для управления:"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ============ ОБНОВЛЕННАЯ ФУНКЦИЯ PORTFOLIO ============

async def show_portfolio_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню портфеля с информацией о лимитах"""
    user_id = update.effective_user.id
    user_data = WalletManager.get_user_wallets(user_id)
    wallets = user_data.get("wallets", {})
    
    current_count = len(wallets)
    max_count = MAX_WALLETS_PER_USER

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить кошелек", callback_data="portfolio:add")],
            [InlineKeyboardButton("👁️ Просмотреть портфель", callback_data="portfolio:view")],
            [InlineKeyboardButton("🔄 Обновить баланс", callback_data="portfolio:refresh")],
        ]
    )

    if wallets:
        keyboard = InlineKeyboardMarkup(
            list(keyboard.inline_keyboard)
            + [[InlineKeyboardButton("🗑 Удалить кошелек", callback_data="portfolio:delete")]]
        )

    text = (
        f"💼 **МОЙ ПОРТФЕЛЬ**\n\n"
        f"📥 Кошельков: **{current_count}/{max_count}**\n\n"
        f"Что хочешь сделать?"
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ============ ОБНОВЛЕННЫЙ MARKET WATCHER ============

async def market_watcher(app: Application):
    """Фоновый мониторинг с улучшенной обработкой ошибок"""
    logger.info("🚀 Market watcher запущен")

    while True:
        try:
            if tracked_tokens:
                async with aiohttp.ClientSession() as session:
                    for address, info in list(tracked_tokens.items()):
                        try:
                            # Rate limit для DexScreener API
                            await rate_limiter.wait_if_needed()
                            
                            subs = info.get("subscribers") or {}
                            if not subs:
                                continue

                            raw = await get_token_pairs_by_address(session, address)
                            pair = pick_best_pair(raw)

                            if not pair:
                                logger.warning(f"Нет пары для {address}")
                                continue

                            # Обработка данных токена...
                            # (существующая логика обработки алертов)

                        except Exception as e:
                            logger.error(f"Ошибка обновления токена {address[:8]}: {e}")
                            continue

                        # Задержка между запросами для соблюдения rate limit
                        await asyncio.sleep(API_RATE_LIMIT_DELAY)

            # Сохраняем состояние токенов периодически
            TokenManager.save_tokens()
            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Критическая ошибка market_watcher: {e}")
            await asyncio.sleep(10)

# ============ ОБНОВЛЕННЫЕ КОМАНДЫ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда start с информацией о лимитах"""
    logger.info(f"/start от {update.effective_user.id}")
    
    await update.message.reply_text(
        "🤖 **Привет! Я крипто-бот для отслеживания токенов и портфеля.**\n\n"
        "📌 **ОСНОВНЫЕ ФУНКЦИИ:**\n"
        f"📋 **Watchlist** — отслеживание до {MAX_TOKENS_PER_USER} токенов\n"
        f"💼 **Мой портфель** — до {MAX_WALLETS_PER_USER} кошельков\n"
        "📊 **Статистика** — общая информация\n\n"
        "⚡ **КОМАНДЫ:**\n"
        "/watchlist — список отслеживаемых токенов\n"
        "/unwatch <адрес> — убрать токен\n"
        "/price — цена BTC\n\n"
        "Используй кнопки меню внизу!",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика с информацией о лимитах"""
    user_id = update.effective_user.id
    
    # Статистика Watchlist
    token_count = TokenManager.get_user_tokens_count(user_id)
    
    # Статистика Портфеля
    user_data = WalletManager.get_user_wallets(user_id)
    wallet_count = len(user_data.get("wallets", {}))
    
    stats_text = f"""
📊 **СТАТИСТИКА:**

🛰️ **WATCHLIST:**
📈 Токенов: {token_count}/{MAX_TOKENS_PER_USER}
🌐 Всего в системе: {len(tracked_tokens)} токенов

💼 **ПОРТФЕЛЬ:**
🪙 Кошельков: {wallet_count}/{MAX_WALLETS_PER_USER}
🌐 Сетей: Solana, Ethereum, Base, BSC

⚡ **СИСТЕМА:**
🛡️ Валидация адресов: ✅
📊 Rate limiting: ✅
💾 Сохранение данных: ✅
    """
    
    await update.message.reply_text(stats_text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

# ============ УТИЛИТЫ (без изменений) ============

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню с кнопками"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить токен"), KeyboardButton("📋 Watchlist")],
            [KeyboardButton("💼 Мой портфель"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("🔗 Инструменты"), KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("❓ Справка")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

def short_addr(address: str) -> str:
    """Сокращает адрес: первые 4 + ... + последние 4 символа"""
    if len(address) <= 10:
        return address
    return f"{address[:4]}...{address[-4:]}"

def ensure_subscriber(info: dict, user_id: int) -> dict:
    """Создает подписчика если не существует"""
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
        # Сохраняем изменения при создании нового подписчика
        TokenManager.save_tokens()

    return sub

# ============ ИНИЦИАЛИЗАЦИЯ ============

async def post_init(app: Application):
    """Инициализация при запуске"""
    logger.info("post_init: запускаем фоновые задачи")
    # Данные уже загружены при инициализации
    asyncio.create_task(market_watcher(app))

def main():
    """Основная функция запуска"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден. Проверь переменную окружения.")
        raise SystemExit("BOT_TOKEN is missing")
    
    # Проверка API ключей
    if not MORALIS_API_KEY:
        logger.warning("⚠️ MORALIS_API_KEY не настроен - портфель EVM будет ограничен")
    
    # Проверка валидности данных при запуске
    logger.info(f"🤖 Загрузка данных: {len(user_wallets)} пользователей, {len(tracked_tokens)} токенов")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("tools", tools))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🤖 Бот запущен с улучшениями безопасности!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
