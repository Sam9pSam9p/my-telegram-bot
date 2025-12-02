# 🚀 ОБНОВЛЕННЫЙ main.py - ВСЕ ИСПРАВЛЕНИЯ ИНТЕГРИРОВАНЫ

"""
main.py - Главный файл крипто-бота с ИИ (ПОЛНОСТЬЮ ОБНОВЛЕННЫЙ)

ВСЕ ИСПРАВЛЕНИЯ DEEPSEEK ВСТРОЕНЫ:
  ✅ БЛОК 1: unified_callback_handler (вместо двойного button_callback)
  ✅ БЛОК 2: TokenManager интегрирован (сохранение watchlist в JSON)
  ✅ БЛОК 3: AddressValidator интегрирован (валидация адресов)
  ✅ БЛОК 4: StateManager интегрирован (управление состояниями)
  ✅ БЛОК 5: SecurityManager интегрирован (rate limiting)
  ✅ БЛОК 6: Moralis интегрирован (с кешем и retry)

ГОТОВО К КОПИРОВАНИЮ НА GITHUB + RAILWAY!
"""

import os
import json
import time
import re
import logging
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ════════════════════════════════════════════════════════════════════════════════
# ИМПОРТЫ КОНФИГУРАЦИИ И MORALIS
# ════════════════════════════════════════════════════════════════════════════════

from config import (
    TELEGRAM_BOT_TOKEN,
    MORALIS_API_KEY,
    GROQ_API_KEY,
    API_REQUEST_TIMEOUT,
)

try:
    from utils_portfolio_service import get_portfolio_service, close_portfolio_service
except ImportError:
    # Fallback если нет Moralis
    get_portfolio_service = None
    close_portfolio_service = None

# ════════════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ════════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════════
# БЛОК 2: TOKEN MANAGER (ИНТЕГРИРОВАН - сохранение в JSON)
# ════════════════════════════════════════════════════════════════════════════════

class TokenManager:
    """Управление сохранением watchlist в JSON файле"""
    
    DATA_FILE = "watchlist.json"
    
    @staticmethod
    def load_tokens() -> Dict[str, Dict]:
        """Загружает токены из JSON файла"""
        try:
            if Path(TokenManager.DATA_FILE).exists():
                with open(TokenManager.DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"✅ Загружено токенов: {len(data)}")
                    return data
            else:
                logger.info("📝 Файл watchlist.json не найден (первый запуск)")
                return {}
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки watchlist: {e}")
            return {}
    
    @staticmethod
    def save_tokens(tokens: Dict[str, Dict]):
        """Сохраняет токены в JSON файл"""
        try:
            with open(TokenManager.DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2, ensure_ascii=False)
            logger.info(f"💾 Сохранено токенов: {len(tokens)}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения watchlist: {e}")
    
    @staticmethod
    def add_token(address: str, token_data: Dict):
        """Добавляет токен в watchlist"""
        try:
            tokens = TokenManager.load_tokens()
            tokens[address] = token_data
            TokenManager.save_tokens(tokens)
            logger.info(f"➕ Токен добавлен: {address[:10]}...")
        except Exception as e:
            logger.error(f"❌ Ошибка добавления токена: {e}")
    
    @staticmethod
    def remove_token(address: str):
        """Удаляет токен из watchlist"""
        try:
            tokens = TokenManager.load_tokens()
            if address in tokens:
                del tokens[address]
                TokenManager.save_tokens(tokens)
                logger.info(f"🗑️ Токен удален: {address[:10]}...")
            else:
                logger.warning(f"⚠️ Токен не найден: {address}")
        except Exception as e:
            logger.error(f"❌ Ошибка удаления токена: {e}")
    
    @staticmethod
    def get_token(address: str) -> Optional[Dict]:
        """Получает данные конкретного токена"""
        try:
            tokens = TokenManager.load_tokens()
            return tokens.get(address)
        except Exception as e:
            logger.error(f"❌ Ошибка получения токена: {e}")
            return None
    
    @staticmethod
    def get_all_tokens() -> Dict[str, Dict]:
        """Получает все токены"""
        return TokenManager.load_tokens()
    
    @staticmethod
    def clear_all():
        """Удаляет все токены"""
        try:
            TokenManager.save_tokens({})
            logger.info("🗑️ Все токены удалены")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки: {e}")
    
    @staticmethod
    def token_exists(address: str) -> bool:
        """Проверяет существует ли токен"""
        tokens = TokenManager.load_tokens()
        return address in tokens
    
    @staticmethod
    def count_tokens() -> int:
        """Возвращает количество токенов"""
        tokens = TokenManager.load_tokens()
        return len(tokens)


# ════════════════════════════════════════════════════════════════════════════════
# БЛОК 3: ADDRESS VALIDATOR (ИНТЕГРИРОВАН - валидация адресов)
# ════════════════════════════════════════════════════════════════════════════════

class AddressValidator:
    """Валидация адресов различных блокчейнов"""
    
    PATTERNS = {
        "evm": r'^0x[a-fA-F0-9]{40}$',
        "solana": r'^[1-9A-HJ-NP-Za-km-z]{32,44}$',
    }
    
    @staticmethod
    def validate(address: str, chain: str = "auto") -> dict:
        """Валидирует адрес блокчейна"""
        address = address.strip()
        
        if not address:
            return {
                "valid": False,
                "error": "❌ Адрес пуст",
                "chain": None,
                "normalized": None
            }
        
        # EVM адреса (Ethereum, Base, BSC)
        if address.startswith("0x"):
            if not re.match(AddressValidator.PATTERNS["evm"], address):
                logger.warning(f"❌ Неверный формат EVM адреса: {address[:10]}...")
                return {
                    "valid": False,
                    "error": "❌ Неверный формат EVM адреса\n"
                             "Должен быть: 0x + 40 hex символов",
                    "chain": None,
                    "normalized": None
                }
            
            logger.info(f"✅ EVM адрес валиден: {address[:10]}...")
            return {
                "valid": True,
                "error": None,
                "chain": "evm",
                "normalized": address.lower()
            }
        
        # Solana адреса
        if re.match(AddressValidator.PATTERNS["solana"], address):
            logger.info(f"✅ Solana адрес валиден: {address[:10]}...")
            return {
                "valid": True,
                "error": None,
                "chain": "solana",
                "normalized": address
            }
        
        logger.warning(f"❌ Неизвестный формат адреса: {address[:10]}...")
        return {
            "valid": False,
            "error": "❌ Неизвестный формат адреса\n\n"
                     "Поддерживаю:\n"
                     "• EVM (0x...)\n"
                     "• Solana (...)",
            "chain": None,
            "normalized": None
        }
    
    @staticmethod
    def is_evm(address: str) -> bool:
        """Проверяет что это EVM адрес"""
        return bool(re.match(AddressValidator.PATTERNS["evm"], address))
    
    @staticmethod
    def is_solana(address: str) -> bool:
        """Проверяет что это Solana адрес"""
        return bool(re.match(AddressValidator.PATTERNS["solana"], address))


# ════════════════════════════════════════════════════════════════════════════════
# БЛОК 4: STATE MANAGER (ИНТЕГРИРОВАН - управление состояниями)
# ════════════════════════════════════════════════════════════════════════════════

class UserState:
    """Состояние пользователя (одного юзера)"""
    
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.action: Optional[str] = None
        self.data: Dict[str, any] = {}
        self.step: int = 0
    
    def reset(self):
        """Очищает состояние"""
        self.action = None
        self.data = {}
        self.step = 0
        logger.debug(f"🔄 Состояние пользователя {self.user_id} очищено")
    
    def update(self, action: Optional[str] = None, 
               data: Optional[Dict] = None, 
               step: Optional[int] = None):
        """Обновляет состояние"""
        if action:
            self.action = action
        if data:
            self.data.update(data)
        if step is not None:
            self.step = step
        
        logger.debug(f"📝 Обновлено состояние {self.user_id}: "
                    f"action={self.action}, step={self.step}")


class StateManager:
    """Управление состояниями всех пользователей"""
    
    def __init__(self):
        self.states: Dict[int, UserState] = {}
        logger.info("🎯 StateManager инициализирован")
    
    def get_state(self, user_id: int) -> UserState:
        """Получает состояние пользователя"""
        if user_id not in self.states:
            self.states[user_id] = UserState(user_id)
            logger.debug(f"👤 Создано новое состояние для {user_id}")
        return self.states[user_id]
    
    def reset_state(self, user_id: int):
        """Сбрасывает состояние пользователя"""
        if user_id in self.states:
            self.states[user_id].reset()
        logger.info(f"🔄 Состояние {user_id} сброшено")
    
    def clear_state(self, user_id: int):
        """Полностью удаляет состояние пользователя"""
        if user_id in self.states:
            del self.states[user_id]
            logger.info(f"🗑️ Состояние {user_id} удалено")
    
    def clear_all(self):
        """Удаляет все состояния"""
        self.states.clear()
        logger.warning("🗑️ ВСЕ состояния удалены!")
    
    def get_all_states(self) -> Dict[int, UserState]:
        """Получает все состояния"""
        return self.states.copy()
    
    def count_active_states(self) -> int:
        """Возвращает количество активных пользователей"""
        return len(self.states)


# ════════════════════════════════════════════════════════════════════════════════
# БЛОК 5: SECURITY MANAGER (ИНТЕГРИРОВАН - rate limiting)
# ════════════════════════════════════════════════════════════════════════════════

class SecurityManager:
    """Rate limiting и защита от спама"""
    
    def __init__(self, max_requests: int = 30, time_window: int = 60):
        """Args: max_requests - максимум запросов, time_window - временное окно"""
        self.max_requests = max_requests
        self.time_window = time_window
        self.user_requests: Dict[int, List[float]] = {}
        logger.info(f"🔐 SecurityManager: {max_requests} запросов в {time_window}с")
    
    async def check_rate_limit(self, user_id: int) -> dict:
        """Проверяет rate limit пользователя"""
        now = time.time()
        
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []
        
        # Удаляем старые запросы
        self.user_requests[user_id] = [
            ts for ts in self.user_requests[user_id]
            if now - ts < self.time_window
        ]
        
        # Проверяем лимит
        if len(self.user_requests[user_id]) >= self.max_requests:
            oldest = self.user_requests[user_id][0]
            retry_in = int(self.time_window - (now - oldest)) + 1
            
            logger.warning(
                f"⚠️ Rate limit для {user_id}: "
                f"{len(self.user_requests[user_id])}/{self.max_requests}"
            )
            
            return {
                "allowed": False,
                "message": f"⚠️ Слишком много запросов. "
                          f"Попробуй через {retry_in} секунд",
                "retry_in": retry_in
            }
        
        # Добавляем текущий запрос
        self.user_requests[user_id].append(now)
        
        return {
            "allowed": True,
            "message": None,
            "retry_in": 0
        }
    
    def get_user_requests_count(self, user_id: int) -> int:
        """Получает количество текущих запросов пользователя"""
        now = time.time()
        
        if user_id not in self.user_requests:
            return 0
        
        active = [
            ts for ts in self.user_requests[user_id]
            if now - ts < self.time_window
        ]
        
        return len(active)
    
    def reset_user(self, user_id: int):
        """Сбрасывает счетчик запросов пользователя"""
        if user_id in self.user_requests:
            self.user_requests[user_id] = []
            logger.info(f"🔄 Rate limit сброшен для {user_id}")


# ════════════════════════════════════════════════════════════════════════════════
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ════════════════════════════════════════════════════════════════════════════════

state_manager = StateManager()
security = SecurityManager(max_requests=30, time_window=60)
token_manager = TokenManager()

# Хранилище пользовательских данных
user_wallets = {}  # {user_id: [addresses]}
user_alerts = {}   # {user_id: {address: {alerts...}}}


# ════════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ МЕНЮ
# ════════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    logger.info(f"👤 Пользователь {user_id} запустил бота")
    
    keyboard = [
        [InlineKeyboardButton("💼 Мой портфель", callback_data="menu:portfolio")],
        [InlineKeyboardButton("👁️ Watchlist", callback_data="menu:watchlist")],
        [InlineKeyboardButton("🤖 Спросить ИИ", callback_data="menu:ai")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 Добро пожаловать в крипто-бот с ИИ помощником!\n\n"
        "Я помогу тебе:\n"
        "✅ Отслеживать портфель\n"
        "✅ Следить за интересующими токенами\n"
        "✅ Получить совет от ИИ\n\n"
        "Выбери действие:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """
    📚 СПРАВКА ПО КОМАНДАМ:
    
    /start - Главное меню
    /help - Эта справка
    /portfolio - Показать портфель
    /add_wallet - Добавить кошелек
    /watchlist - Список отслеживаемых токенов
    /ai - Спросить совет у ИИ
    
    💡 Используй кнопки меню для удобства!
    """
    await update.message.reply_text(help_text)


# ════════════════════════════════════════════════════════════════════════════════
# ПОРТФЕЛЬ (БЛОК 6: MORALIS ИНТЕГРИРОВАН)
# ════════════════════════════════════════════════════════════════════════════════

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать портфель пользователя"""
    user_id = update.effective_user.id
    
    # Проверка rate limit
    check = await security.check_rate_limit(user_id)
    if not check["allowed"]:
        await update.message.reply_text(check["message"])
        return
    
    wallets = user_wallets.get(user_id, [])
    
    if not wallets:
        await update.message.reply_text(
            "❌ У тебя нет добавленных кошельков\n\n"
            "Добавь кошелек: /add_wallet"
        )
        return
    
    await update.message.reply_text("⏳ Загружаю портфель...")
    
    if not get_portfolio_service:
        await update.message.reply_text("❌ Moralis API не настроена")
        return
    
    service = await get_portfolio_service()
    
    for address in wallets:
        try:
            portfolio = await service.get_portfolio(address, "ethereum")
            
            if portfolio:
                text = service.format_portfolio(portfolio)
                await update.message.reply_text(text, parse_mode="Markdown")
            else:
                await update.message.reply_text(
                    f"❌ Не удалось загрузить портфель {address[:10]}..."
                )
        except Exception as e:
            logger.error(f"Ошибка при загрузке портфеля: {e}")
            await update.message.reply_text(
                f"❌ Ошибка: {str(e)[:100]}"
            )


async def add_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс добавления кошелька"""
    user_id = update.effective_user.id
    
    state = state_manager.get_state(user_id)
    state.update(action="add_wallet", step=1)
    
    await update.message.reply_text(
        "📝 Введи адрес кошелька:\n\n"
        "Примеры:\n"
        "• EVM (Ethereum/Base/BSC): 0x...\n"
        "• Solana: ...\n\n"
        "Или /cancel для отмены"
    )


async def process_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введённого адреса кошелька (БЛОК 6 + БЛОК 3)"""
    user_id = update.effective_user.id
    address = update.message.text.strip()
    
    state = state_manager.get_state(user_id)
    
    if state.action != "add_wallet" or state.step != 1:
        return
    
    # Валидируем адрес (БЛОК 3)
    result = AddressValidator.validate(address)
    
    if not result["valid"]:
        await update.message.reply_text(result["error"])
        return
    
    # Адрес валидный
    address = result["normalized"]
    chain = result["chain"]
    
    state.update(
        data={"address": address, "chain": chain},
        step=2
    )
    
    # Проверяем портфель (БЛОК 6)
    await update.message.reply_text("⏳ Проверяю портфель...")
    
    if not get_portfolio_service:
        await update.message.reply_text("❌ Moralis API не настроена")
        state_manager.reset_state(user_id)
        return
    
    service = await get_portfolio_service()
    portfolio = await service.get_portfolio(address, chain)
    
    if portfolio:
        text = service.format_portfolio(portfolio)
        await update.message.reply_text(text, parse_mode="Markdown")
        
        # Сохраняем кошелек
        if user_id not in user_wallets:
            user_wallets[user_id] = []
        
        if address not in user_wallets[user_id]:
            user_wallets[user_id].append(address)
        
        await update.message.reply_text(
            f"✅ Кошелек {address[:10]}... добавлен!\n\n"
            "Выбери действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Не удалось загрузить портфель\n"
            "Проверь адрес и попробуй снова"
        )
    
    state_manager.reset_state(user_id)


# ════════════════════════════════════════════════════════════════════════════════
# WATCHLIST (БЛОК 2: TOKENMANAGER ИНТЕГРИРОВАН)
# ════════════════════════════════════════════════════════════════════════════════

async def show_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список отслеживаемых токенов"""
    user_id = update.effective_user.id
    
    tokens = token_manager.get_all_tokens()
    
    if not tokens:
        keyboard = [
            [InlineKeyboardButton("➕ Добавить токен", callback_data="watchlist:add")],
        ]
        await update.message.reply_text(
            "📭 Watchlist пуст\n\n"
            "Добавь интересующие токены для отслеживания",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    text = "👁️ МОЙ WATCHLIST\n\n"
    for address, data in list(tokens.items())[:10]:
        symbol = data.get('symbol', '???')
        text += f"• {symbol} ({address[:10]}...)\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить", callback_data="watchlist:add")],
        [InlineKeyboardButton("🗑️ Очистить", callback_data="watchlist:clear")],
    ]
    
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ════════════════════════════════════════════════════════════════════════════════
# БЛОК 1: ЕДИНЫЙ ОБРАБОТЧИК CALLBACK'ОВ (ИНТЕГРИРОВАН - заменяет двойной button_callback)
# ════════════════════════════════════════════════════════════════════════════════

async def unified_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ГЛАВНЫЙ роутер для всех callback'ов (БЛОК 1)
    Заменяет двойной button_callback из старого кода
    
    Поддерживаемые префиксы:
      - menu:*        (главное меню)
      - portfolio:*   (портфель)
      - watchlist:*   (отслеживаемые токены)
      - ai:*          (ИИ функции)
      - select_*      (выбор из списка)
    """
    query = update.callback_query
    data = query.data or ""
    
    try:
        await query.answer()
    except:
        pass
    
    user_id = update.effective_user.id
    logger.info(f"👤 {user_id} нажал: {data}")
    
    # ══════════════════════════════════════════════════════════════════════════
    # МАРШРУТ 1: МЕНЮ CALLBACK'Ы
    # ══════════════════════════════════════════════════════════════════════════
    
    if data == "menu:portfolio":
        await show_portfolio(update, context)
    
    elif data == "menu:watchlist":
        await show_watchlist(update, context)
    
    elif data == "menu:ai":
        state = state_manager.get_state(user_id)
        state.update(action="ask_ai", step=1)
        await query.edit_message_text(
            "🤖 Спросите что-нибудь о криптовалютах или рынке:\n\n"
            "(Введите вопрос в чат)"
        )
    
    elif data == "menu:settings":
        await query.edit_message_text(
            "⚙️ НАСТРОЙКИ\n\n"
            "🔧 В разработке...",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu:back")]
            ])
        )
    
    elif data == "menu:back":
        await query.edit_message_text(
            "👋 Главное меню",
            reply_markup=get_main_keyboard()
        )
    
    # ══════════════════════════════════════════════════════════════════════════
    # МАРШРУТ 2: PORTFOLIO CALLBACK'Ы
    # ══════════════════════════════════════════════════════════════════════════
    
    elif data.startswith("portfolio:"):
        action = data.replace("portfolio:", "")
        logger.info(f"Portfolio action: {action}")
        await query.edit_message_text("📊 Portfolio обновляется...")
    
    # ══════════════════════════════════════════════════════════════════════════
    # МАРШРУТ 3: WATCHLIST CALLBACK'Ы
    # ══════════════════════════════════════════════════════════════════════════
    
    elif data == "watchlist:add":
        state = state_manager.get_state(user_id)
        state.update(action="add_token", step=1)
        await query.edit_message_text(
            "📝 Введи адрес токена для отслеживания:\n\n"
            "Примеры: 0x..., или адрес Solana"
        )
    
    elif data == "watchlist:clear":
        token_manager.clear_all()
        await query.edit_message_text(
            "🗑️ Watchlist очищен!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu:back")]
            ])
        )
    
    elif data.startswith("watchlist:"):
        action = data.replace("watchlist:", "")
        logger.info(f"Watchlist action: {action}")
    
    # ══════════════════════════════════════════════════════════════════════════
    # МАРШРУТ 4: AI CALLBACK'Ы
    # ══════════════════════════════════════════════════════════════════════════
    
    elif data.startswith("ai:"):
        action = data.replace("ai:", "")
        logger.info(f"AI action: {action}")
    
    # ══════════════════════════════════════════════════════════════════════════
    # МАРШРУТ 5: SELECT CALLBACK'Ы
    # ══════════════════════════════════════════════════════════════════════════
    
    elif data.startswith("select_"):
        action = data.replace("select_", "")
        logger.info(f"Select action: {action}")
    
    # ══════════════════════════════════════════════════════════════════════════
    # НЕИЗВЕСТНОЕ ДЕЙСТВИЕ
    # ══════════════════════════════════════════════════════════════════════════
    
    else:
        logger.warning(f"Unknown callback: {data}")
        await query.edit_message_text("❌ Неизвестное действие")


# ════════════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ
# ════════════════════════════════════════════════════════════════════════════════

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех текстовых сообщений"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Проверка rate limit
    check = await security.check_rate_limit(user_id)
    if not check["allowed"]:
        await update.message.reply_text(check["message"])
        return
    
    # Получаем состояние пользователя
    state = state_manager.get_state(user_id)
    
    # ══════════════════════════════════════════════════════════════════════════
    # ДОБАВЛЕНИЕ КОШЕЛЬКА (БЛОК 3 + БЛОК 6)
    # ══════════════════════════════════════════════════════════════════════════
    
    if state.action == "add_wallet" and state.step == 1:
        await process_wallet_address(update, context)
        return
    
    # ══════════════════════════════════════════════════════════════════════════
    # ДОБАВЛЕНИЕ ТОКЕНА В WATCHLIST (БЛОК 2 + БЛОК 3)
    # ══════════════════════════════════════════════════════════════════════════
    
    if state.action == "add_token" and state.step == 1:
        result = AddressValidator.validate(text)
        
        if not result["valid"]:
            await update.message.reply_text(result["error"])
            return
        
        address = result["normalized"]
        token_manager.add_token(
            address,
            {"address": address, "symbol": "???", "added_at": datetime.now().isoformat()}
        )
        
        await update.message.reply_text(
            f"✅ Токен {address[:10]}... добавлен в watchlist!"
        )
        state_manager.reset_state(user_id)
        return
    
    # ══════════════════════════════════════════════════════════════════════════
    # ВОПРОС К ИИ
    # ══════════════════════════════════════════════════════════════════════════
    
    if state.action == "ask_ai":
        await update.message.reply_text(
            "🤖 Думаю...\n\n"
            "(ИИ обработка в разработке)"
        )
        state_manager.reset_state(user_id)
        return
    
    # ══════════════════════════════════════════════════════════════════════════
    # НЕИЗВЕСТНОЕ СООБЩЕНИЕ
    # ══════════════════════════════════════════════════════════════════════════
    
    await update.message.reply_text(
        "❓ Не понимаю\n\n"
        "Используй /start или /help"
    )


# ════════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════════════════════════

def get_main_keyboard():
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("💼 Мой портфель", callback_data="menu:portfolio")],
        [InlineKeyboardButton("👁️ Watchlist", callback_data="menu:watchlist")],
        [InlineKeyboardButton("🤖 Спросить ИИ", callback_data="menu:ai")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"❌ Ошибка: {context.error}")


async def shutdown_handler(app):
    """Выполнится при завершении (БЛОК 6)"""
    logger.info("🛑 Завершаю работу...")
    if close_portfolio_service:
        try:
            await close_portfolio_service()
        except:
            pass
    logger.info("✅ Бот завершил работу")


# ════════════════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ
# ════════════════════════════════════════════════════════════════════════════════

def main():
    """Главная функция запуска бота"""
    
    # Проверяем конфигурацию
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен в .env!")
        return
    
    if not MORALIS_API_KEY:
        logger.warning("⚠️ MORALIS_API_KEY не установлен - портфель не будет работать")
    
    logger.info("🚀 Запускаю бота...")
    
    # Создаём приложение
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # ══════════════════════════════════════════════════════════════════════════
    # РЕГИСТРИРУЕМ ОБРАБОТЧИКИ
    # ══════════════════════════════════════════════════════════════════════════
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("portfolio", show_portfolio))
    app.add_handler(CommandHandler("add_wallet", add_wallet_handler))
    app.add_handler(CommandHandler("watchlist", show_watchlist))
    
    # Callback'ы (ГЛАВНЫЙ ОБРАБОТЧИК - БЛОК 1)
    app.add_handler(CallbackQueryHandler(unified_callback_handler))
    
    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Ошибки
    app.add_error_handler(error_handler)
    
    # Завершение (БЛОК 6)
    app.post_shutdown(shutdown_handler)
    
    logger.info("✅ Обработчики зарегистрированы")
    logger.info("📡 Бот готов к работе!")
    
    # Запускаем
    app.run_polling()


if __name__ == '__main__':
    main()
