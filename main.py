import os
import time
import logging
import asyncio
import json
from collections import deque
from dotenv import load_dotenv

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

# ============ ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ============

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
BASESCAN_API_KEY = os.getenv("BASESCAN_API_KEY", "")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

AI_PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",  # новая модель
        "key": GROQ_API_KEY,
        "label": "Groq Llama 3.3",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-8b-instruct",
        "key": OPENROUTER_API_KEY,
        "label": "OpenRouter Llama 3.1",
    },
}

# ============ НАСТРОЙКИ ============

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Глобальные переменные WATCHLIST
tracked_tokens: dict[str, dict] = {}
pending_threshold_input: dict[int, dict] = {}

# Глобальные переменные ПОРТФЕЛЯ
user_wallets: dict[int, dict] = {}
pending_wallet_input: dict[int, dict] = {}
DATA_FILE = "bot_data.json"

PORTFOLIO_UPDATE_INTERVAL = 600  # 10 минут
PORTFOLIO_LAST_UPDATE = {}


# ============ ФУНКЦИИ JSON ХРАНИЛИЩА ============

def load_data():
    """Загружает данные из bot_data.json"""
    global user_wallets
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            user_wallets = {int(k): v for k, v in data.items()}
            logger.info(f"📊 Данные загружены: {len(user_wallets)} пользователей")
    except FileNotFoundError:
        user_wallets = {}
        logger.info("📊 Новое хранилище создано")

def save_data():
    """Сохраняет данные в bot_data.json"""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(user_wallets, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def get_user_wallets(user_id: int) -> dict:
    """Получает кошельки пользователя"""
    if user_id not in user_wallets:
        user_wallets[user_id] = {"wallets": {}, "last_update": 0}
        save_data()
    return user_wallets[user_id]


# ============ ФУНКЦИИ ПОЛУЧЕНИЯ БАЛАНСА ============

async def get_solana_balance(address: str) -> dict:
    """Получает баланс кошелька Solana"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(SOLANA_RPC, json=payload, timeout=aiohttp.ClientTimeout(5)) as resp:
                data = await resp.json()
                balance_lamports = data.get("result", {}).get("value", 0)
                balance_sol = balance_lamports / 1e9
                
                # Получаем цену SOL
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") as price_resp:
                    price_data = await price_resp.json()
                    sol_price = price_data.get("solana", {}).get("usd", 0)
                
                return {
                    "balance": round(balance_sol, 4),
                    "usd_value": round(balance_sol * sol_price, 2),
                    "price": sol_price
                }
    except Exception as e:
        logger.error(f"❌ Ошибка Solana баланса: {e}")
        return {"balance": 0, "usd_value": 0, "price": 0}
async def get_evm_portfolio_moralis(address: str, chain: str = "ethereum") -> dict:
    """
    Получает EVM-портфель через Moralis Wallet API:
    native + токены, итоговую сумму в USD.
    """
    if not MORALIS_API_KEY:
        logger.warning("⚠️ MORALIS_API_KEY is missing")
        return {"balance": 0, "usd_value": 0, "tokens": []}

    chain_map = {
        "ethereum": "eth",
        "base": "base",
        "bsc": "bsc",
    }

    moralis_chain = chain_map.get(chain)
    if not moralis_chain:
        logger.warning(f"⚠️ Moralis: unsupported chain={chain}")
        return {"balance": 0, "usd_value": 0, "tokens": []}

    # Получаем нативный баланс
    url_native = f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/balance"
    headers = {
        "X-API-Key": MORALIS_API_KEY,
        "accept": "application/json",
    }

    native_usd = 0.0
    native_balance = 0.0

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(15)) as session:
            # Нативный баланс
            params_native = {"chain": moralis_chain}
            async with session.get(url_native, params=params_native, headers=headers) as resp:
                native_data = await resp.json()
                native_balance_wei = float(native_data.get("balance") or 0)
                native_balance = native_balance_wei / 1e18
                native_usd = float(native_data.get("usd_value") or 0)
    except Exception as e:
        logger.error(f"⚠️ Moralis native balance error for {chain} {address}: {e}")
        native_balance = 0.0

    # Получаем токены
    url_tokens = f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/tokens"
    tokens = []
    tokens_usd = 0.0

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(20)) as session:
            params_tokens = {
                "chain": moralis_chain,
                "exclude_spam": "true",
            }
            async with session.get(url_tokens, params=params_tokens, headers=headers) as resp:
                data = await resp.json()
                if isinstance(data, list):
                    for t in data:
                        try:
                            symbol = t.get("symbol") or ""
                            name = t.get("name") or ""
                            balance = float(t.get("balance_formatted") or t.get("balance") or 0)
                            usd_value = float(t.get("usd_value") or 0)
                            tokens_usd += usd_value

                            tokens.append({
                                "symbol": symbol,
                                "name": name,
                                "balance": balance,
                                "usd_value": usd_value,
                            })
                        except Exception:
                            continue
    except Exception as e:
        logger.error(f"⚠️ Moralis tokens error for {chain} {address}: {e}")

    total_usd = native_usd + tokens_usd

    logger.info(
        f"Moralis portfolio chain={chain} addr={short_addr(address)} "
        f"native={native_balance:.4f} tokens_count={len(tokens)} total_usd={total_usd}"
    )

    return {
        "balance": round(native_balance, 6),
        "usd_value": round(total_usd, 2),
        "tokens": tokens,
    }



async def get_evm_balance(address: str, chain: str = "ethereum") -> dict:
    """Получает баланс нативной монеты через Etherscan V2 мультичейн."""
    try:
        api_key = ETHERSCAN_API_KEY
        if not api_key:
            logger.warning(f"⚠️ EVM V2: API key missing for chain={chain}")
            return {"balance": 0, "usd_value": 0, "price": 0}

        # соответствие chain -> chainid для V2
        chain_ids = {
            "ethereum": 1,   # Ethereum mainnet
            "base": 8453,    # Base mainnet
            "bsc": 56,       # BNB Smart Chain (если поддерживается планом)
        }
        coin_ids = {
            "ethereum": ("ethereum", "ETH"),
            "base": ("ethereum", "ETH"),
            "bsc": ("binancecoin", "BNB"),
        }

        chainid = chain_ids.get(chain)
        if not chainid:
            logger.warning(f"⚠️ EVM V2: unsupported chain={chain}")
            return {"balance": 0, "usd_value": 0, "price": 0}

        coin_id, symbol = coin_ids.get(chain, ("ethereum", "ETH"))

        base_url = "https://api.etherscan.io/v2/api"

        params = {
            "chainid": chainid,
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
            "apikey": api_key,
        }

        logger.info(f"EVM V2 request: chain={chain}, url={base_url}, params={params}")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(15)) as session:
            async with session.get(base_url, params=params) as resp:
                data = await resp.json()

            status = str(data.get("status"))
            message = data.get("message")
            result = data.get("result")

            logger.info(
                f"EVM V2 response: chain={chain}, status={status}, message={message}, "
                f"result_preview={str(result)[:80]}"
            )

            if status != "1" or message != "OK":
                logger.warning(
                    f"⚠️ EVM V2: API error for chain={chain}, addr={address}: "
                    f"status={status}, message={message}, result={result}"
                )
                return {"balance": 0, "usd_value": 0, "price": 0}

            balance_wei = int(result)
            balance = balance_wei / 1e18

            # получаем цену с Coingecko
            async with session.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
            ) as price_resp:
                price_data = await price_resp.json()
                coin_price = price_data.get(coin_id, {}).get("usd", 0)

        return {
            "balance": round(balance, 4),
            "usd_value": round(balance * coin_price, 2),
            "price": coin_price,
        }

    except Exception as e:
        logger.error(f"⚠️ EVM V2 balance error for {chain}: {e}")
        return {"balance": 0, "usd_value": 0, "price": 0}

# ============ УТИЛИТЫ ============

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


def short_addr(address: str) -> str:
    """Сокращает адрес: первые 4 + ... + последние 4 символа"""
    if len(address) <= 10:
        return address
    return f"{address[:4]}...{address[-4:]}"


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
    """Главное меню с кнопками"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить токен"), KeyboardButton("📋 Watchlist")],
            [KeyboardButton("💼 Мой портфель"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("🤖 ИИ помощник"), KeyboardButton("🔗 Инструменты")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("❓ Справка")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )



def detect_pump_dump(history: deque) -> str:
    """Анализирует памп/дамп"""
    if len(history) < 3:
        return ""

    recent = list(history)[-5:]
    buy_vols = [b for _, b, _ in recent]
    sell_vols = [s for _, _, s in recent]

    avg_buy = sum(buy_vols) / len(buy_vols) if buy_vols else 0
    avg_sell = sum(sell_vols) / len(sell_vols) if sell_vols else 0

    if buy_vols and buy_vols[-1] > avg_buy * 2.5:
        return "📈 Возможный памп (высокий buy объём)"
    
    if sell_vols and sell_vols[-1] > avg_sell * 2.5:
        return "📉 Возможный дамп (высокий sell объём)"
    
    return ""

async def call_text_ai(provider: str, prompt: str) -> str:
    """Вызов текстовой модели (Groq или OpenRouter)."""
    cfg = AI_PROVIDERS.get(provider)
    if not cfg or not cfg.get("key"):
        return f"❌ Модель {provider} недоступна (нет API ключа)."

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['key']}",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://yourbot.example"
        headers["X-Title"] = "Your Telegram Bot"

    body = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты ассистент-криптоаналитик. Отвечай кратко и по делу, "
                    "используя данные портфеля и watchlist пользователя."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 800,
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(20)) as session:
            async with session.post(cfg["url"], headers=headers, json=body) as resp:
                data = await resp.json()
    except Exception as e:
        logger.error(f"AI {provider} error: {e}")
        return f"❌ Ошибка запроса к {provider}: {e}"

    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        logger.error(f"Unexpected AI response {provider}: {data}")
        return "❌ Не удалось разобрать ответ модели."


async def get_user_context(user_id: int) -> str:
    """Контекст по портфелю и watchlist для промпта ИИ."""
    udata = get_user_wallets(user_id)
    wallets = udata.get("wallets", {})

    # ПОРТФЕЛЬ
    portfolio_text = ""
    if wallets:
        portfolio_text = "📊 **ПОРТФЕЛЬ:**\n"
        total_portfolio_usd = 0.0
        for wallet_id, w in wallets.items():
            chain = w.get("chain", "unknown").upper()
            name = w.get("name", chain)
            balance = float(w.get("balance", 0) or 0)
            usd = float(w.get("usd_value", 0) or 0)
            total_portfolio_usd += usd
            portfolio_text += f"  • {name} ({chain}): {balance:.4f} ≈ ${usd:,.2f}\n"

        portfolio_text += f"  **ИТОГО: ${total_portfolio_usd:,.2f}**\n\n"
    else:
        portfolio_text = "📊 **ПОРТФЕЛЬ:** Пуст\n\n"

    # WATCHLIST
    watchlist_text = "🛰️ **WATCHLIST:**\n"
    has_active_watchlist = False
    for address, info in tracked_tokens.items():
        sub = info.get("subscribers", {}).get(user_id)
        if not sub:
            continue

        symbol = info.get("symbol", "?")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")
        vt = sub.get("vol_threshold")

        if pt is not None or mt is not None or vt is not None:
            has_active_watchlist = True
            params = []
            if pt is not None:
                params.append(f"цена {pt:.1f}%")
            if mt is not None:
                params.append(f"капа {mt:.1f}%")
            if vt is not None:
                params.append(f"объём {vt:.1f}%")
            watchlist_text += f"  • {symbol}: {', '.join(params)}\n"

        if not has_active_watchlist:
        watchlist_text += "  (нет активных отслеживаний)\n"

    return portfolio_text + watchlist_text



# ============ КОМАНДЫ ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start от {update.effective_user.id}")
    load_data()
    await update.message.reply_text(
        "🤖 **Привет! Я крипто-бот для отслеживания токенов и портфеля.**\n\n"
        "📌 **ОСНОВНЫЕ ФУНКЦИИ:**\n"
        "📋 **Watchlist** — отслеживание токенов с алертами\n"
        "💼 **Мой портфель** — управление кошельками (Solana, ETH, Base, BSC)\n"
        "📊 **Статистика** — общая информация\n\n"
        "⚡ **КОМАНДЫ:**\n"
        "/watchlist — список отслеживаемых токенов\n"
        "/unwatch <адрес> — убрать токен\n"
        "/price — цена BTC\n\n"
        "Используй кнопки меню внизу!",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ **КАК ИСПОЛЬЗОВАТЬ БОТ:**\n\n"
        "📈 **WATCHLIST:**\n"
        "• Отправь адрес токена\n"
        "• Выбери параметры (цена, капа, объём)\n"
        "• Получай алерты в реальном времени\n\n"
        "💼 **ПОРТФЕЛЬ:**\n"
        "• Добавь кошельки из 4 сетей\n"
        "• Просматривай баланс и историю\n"
        "• Обновляй баланс кнопкой\n\n"
        "🌐 **ПОДДЕРЖИВАЕМЫЕ СЕТИ:**\n"
        "🔹 Solana\n"
        "🔹 Ethereum\n"
        "🔹 Base\n"
        "🔹 BSC (Binance Smart Chain)\n\n"
        "💡 **СОВЕТ:** Начни с малых порогов (5-10%) в Watchlist!",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/price от {update.effective_user.id}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            ) as resp:
                data = await resp.json()

        btc_price = data["bitcoin"]["usd"]
        await update.message.reply_text(
            f"₿ **Bitcoin:** ${btc_price:,.2f}", 
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Ошибка /price: {e}")
        await update.message.reply_text("❌ Ошибка получения цены BTC")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    user_id = update.effective_user.id
    
    # Статистика Watchlist
    total_tokens = 0
    active_tokens = 0
    disabled_tokens = 0
    
    for address, info in tracked_tokens.items():
        sub = info.get("subscribers", {}).get(user_id)
        if not sub:
            continue
        
        total_tokens += 1
        
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")
        vt = sub.get("vol_threshold")
        
        if pt is not None or mt is not None or vt is not None:
            active_tokens += 1
        else:
            disabled_tokens += 1
    
    # Статистика Портфеля
    user_data = get_user_wallets(user_id)
    wallet_count = len(user_data.get("wallets", {}))
    
    stats_text = f"""
📊 **СТАТИСТИКА:**

🛰️ **WATCHLIST:**
📈 Всего токенов: {total_tokens}
🟢 Активных: {active_tokens}
⚫ В списке (без алертов): {disabled_tokens}

💼 **ПОРТФЕЛЬ:**
🪙 Кошельков: {wallet_count}
🌐 Сетей: Solana, Ethereum, Base, BSC

💡 Совет: Используй /watchlist и 💼 Мой портфель для управления!
    """
    
    await update.message.reply_text(stats_text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")


async def tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инструменты и ссылки"""
    tools_text = """
🔗 **БЫСТРЫЕ ИНСТРУМЕНТЫ:**

📊 **АНАЛИТИКА:**
• DexScreener: https://dexscreener.com
• Birdeye: https://birdeye.so
• Defined.fi: https://defined.fi

🔍 **СКАНЕРЫ БЛОКЧЕЙНА:**
• Solscan: https://solscan.io
• Etherscan: https://etherscan.io
• BaseScan: https://basescan.org
• BscScan: https://bscscan.com

⚠️ **БЕЗОПАСНОСТЬ:**
• Rugscreen: https://rugscreen.com
• TokenSense: https://tokensense.io
    """
    
    await update.message.reply_text(tools_text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки"""
    settings_text = """
⚙️ **НАСТРОЙКИ:**

🚀 В разработке:
• Профиль пользователя
• Язык интерфейса
• Часовой пояс
• Пороги уведомлений по умолчанию
• Тихий режим
• Приоритет сигналов

Скоро будут доступны!
    """
    
    await update.message.reply_text(settings_text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ai — выбор модели и запрос."""
    user_id = update.effective_user.id
    text = " ".join(context.args).strip()

    active = {k: v for k, v in AI_PROVIDERS.items() if v.get("key")}
    if not active:
        await update.message.reply_text(
            "❌ Нет доступных AI моделей. Проверь GROQ_API_KEY и OPENROUTER_API_KEY.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if not text:
        labels = ", ".join(v["label"] for v in active.values())
        await update.message.reply_text(
            "🤖 Использование: `/ai твой вопрос`\n\n"
            "Примеры:\n"
            "• `/ai проанализируй мой портфель`\n"
            "• `/ai оцени риски токенов из watchlist`\n"
            "• `/ai предложи пороги алертов по цене и объёму`.\n\n"
            f"Доступные модели: {labels}",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # сохраним полный вопрос для колбэка
    short_query = text[:150]
    context.user_data["last_ai_query"] = short_query

    user_ctx = await get_user_context(user_id)

    rows = []
    if "groq" in active:
        rows.append(
            [InlineKeyboardButton("🆓 Groq (Llama 3.3)", callback_data="ai:groq")]
        )
    if "openrouter" in active:
        rows.append(
            [InlineKeyboardButton("🆓 OpenRouter Llama", callback_data="ai:openrouter")]
        )
    if len(rows) > 1:
        rows.append(
            [InlineKeyboardButton("🎯 Mix (автовыбор)", callback_data="ai:mix")]
        )

    keyboard = InlineKeyboardMarkup(rows)

    await update.message.reply_text(
        f"🤖 Запрос: `{text}`\n"
        f"📊 Контекст: {user_ctx}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )



async def ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки AI."""
    q = update.callback_query
    data = q.data or ""
    user_id = q.from_user.id

    if not data.startswith("ai:"):
        return

    # из callback берём только провайдера
    try:
        _, provider = data.split(":", 1)
    except ValueError:
        await q.answer("Некорректная кнопка.")
        return

    # сам вопрос берём из user_data
    short_query = (context.user_data.get("last_ai_query") or "").strip()
    if not short_query:
        await q.answer("Вопрос для ИИ не найден, попробуй ещё раз через /ai.")
        return


    # Mix: выбираем модель автоматически
    if provider == "mix":
        has_groq = bool(AI_PROVIDERS.get("groq", {}).get("key"))
        has_or = bool(AI_PROVIDERS.get("openrouter", {}).get("key"))
        low = short_query.lower()
        if ("код" in low or "contract" in low or "script" in low) and has_or:
            provider = "openrouter"
        elif has_groq:
            provider = "groq"
        elif has_or:
            provider = "openrouter"
        else:
            await q.answer("Нет активных моделей.")
            return

    await q.answer("🤖 Думаю...")
    await q.edit_message_text("🤖 Генерирую ответ...")

    user_ctx = await get_user_context(user_id)
    full_prompt = f"{user_ctx}\n\nВопрос пользователя: {short_query}"

    answer = await call_text_ai(provider, full_prompt)
    label = AI_PROVIDERS.get(provider, {}).get("label", provider)


    await q.edit_message_text(
        f"**{label}:**\n\n{answer}",
        parse_mode="Markdown",
        reply_markup=None,
    )

    # ========== ОЧИЩАЕМ ФЛАГИ ИИ ==========
    context.user_data.pop("awaiting_ai_question", None)
    context.user_data.pop("last_ai_query", None)
    context.user_data.pop("last_token_addr", None)
    # =========================================

# ============ КОМАНДЫ ПОРТФЕЛЯ ============

async def show_portfolio_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню портфеля"""
    user_id = update.effective_user.id
    user_data = get_user_wallets(user_id)
    wallets = user_data.get("wallets", {})

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

    count = len(wallets)
    text = (
        f"💼 **МОЙ ПОРТФЕЛЬ**\n\n"
        f"📥 Кошельков добавлено: **{count}**\n\n"
        f"Что хочешь сделать?"
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")



async def view_portfolio_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр полного портфеля"""
    user_id = update.effective_user.id
    message = update.effective_message
    user_data = get_user_wallets(user_id)
    wallets = user_data.get("wallets", {})

    if not wallets:
        await message.reply_text(
            "💼 Твой портфель пуст!\n\n➕ Добавь кошелек, чтобы начать отслеживание.",
            reply_markup=main_menu_keyboard(),
        )
        return

    text = "💼 **Твой ПОРТФЕЛЬ:**\n\n"
    total_usd = 0

    for wallet_id, wallet_info in wallets.items():
        addr = wallet_info.get("address", "")
        chain = wallet_info.get("chain", "")
        name = wallet_info.get("name", chain)
        balance = wallet_info.get("balance", 0)
        usd = wallet_info.get("usd_value", 0)

        total_usd += usd
        emoji = {"solana": "🟣", "ethereum": "⚪", "base": "🔵", "bsc": "🟡"}.get(chain, "💫")

        text += f"{emoji} **{name}** ({chain.upper()})\n"
        text += f"   💰 {balance:.4f} | ${usd:,.2f}\n"
        text += f"   {short_addr(addr)}\n\n"

    text += f"**━━━━━━━━━━━━━━━━━━━━**\n"
    text += f"**ИТОГО: ${total_usd:,.2f}**"

    await message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")



# ============ ОБРАБОТКА СООБЩЕНИЙ ============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    logger.info(f"MSG от {user_id}: {text[:80]}")

    # ========== КНОПКИ ГЛАВНОГО МЕНЮ (ГЛАВНЫЙ ПРИОРИТЕТ) ==========

    if text == "📋 Watchlist":
        await watchlist(update, context)
        return

    if text == "🤖 ИИ помощник":
        context.user_data["awaiting_ai_question"] = True
        context.user_data.pop("last_token_addr", None)
        await update.message.reply_text(
            "🤖 Напиши свой вопрос для ИИ.\n"
            "Можешь без /ai, просто текст.\n"
            "Например: `проанализируй мой портфель и риски`.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if text == "💼 Мой портфель":
        await show_portfolio_menu(update, context)
        return

    if text == "❓ Справка":
        await help_cmd(update, context)
        return

    if text == "📊 Статистика":
        await stats(update, context)
        return

    if text == "🔗 Инструменты":
        await tools(update, context)
        return

    if text == "⚙️ Настройки":
        await settings(update, context)
        return

    if text == "➕ Добавить токен":
        await update.message.reply_text(
            "📍 Отправь адрес контракта токена, который хочешь отслеживать.\n\n"
            "Примеры:\n"
            "• Solana: EPjFWaLb3odcccccccccccccccccccccccccccccccccc\n"
            "• Ethereum: 0xdAC17F958D2ee523a2206206994597C13D831ec7 (USDT)\n"
            "• Base: 0x833589fCD6eDb6E08f4c7C32D4f71b1566dA3633 (USDC)",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ========== ЖДЁМ ВОПРОС ДЛЯ ИИ ==========

    if context.user_data.get("awaiting_ai_question"):
        # Отправляем в /ai как будто использовал команду
        context.args = text.split()
        context.user_data["awaiting_ai_question"] = False
        await ai_chat(update, context)
        return

    # ========== ПОРТФЕЛЬ: ВВОД АДРЕСА КОШЕЛЬКА ==========

    if user_id in pending_wallet_input:
        state = pending_wallet_input[user_id]
        if text == "Отмена":
            pending_wallet_input.pop(user_id, None)
            await update.message.reply_text("❌ Отмена", reply_markup=main_menu_keyboard())
            return

        if state.get("step") == "address":
            if len(text) < 30:
                await update.message.reply_text(
                    "❌ Адрес слишком короткий. Проверь и отправь снова.",
                    reply_markup=main_menu_keyboard()
                )
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

        if state.get("step") == "chain":
            chain_map = {
                "solana": "solana",
                "ethereum": "ethereum",
                "base": "base",
                "bsc": "bsc"
            }

            chain = chain_map.get(text.lower())
            if not chain:
                await update.message.reply_text(
                    "❌ Выбери из предложенных вариантов.",
                    reply_markup=main_menu_keyboard()
                )
                return

            state["chain"] = chain
            state["step"] = "name"

            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("Отмена")]],
                resize_keyboard=True,
                one_time_keyboard=True
            )

            await update.message.reply_text(
                "📝 Введи название для этого кошелька (например: 'Основной', 'Trading'):",
                reply_markup=keyboard
            )
            return

        if state.get("step") == "name":
            address = state["address"]
            chain = state["chain"]
            name = text if text != "Отмена" else chain.capitalize()

            user_data = get_user_wallets(user_id)
            wallet_id = f"wallet_{len(user_data['wallets']) + 1}"

            user_data["wallets"][wallet_id] = {
                "address": address,
                "chain": chain,
                "name": name,
                "added_at": int(time.time()),
                "balance": 0,
                "usd_value": 0,
                "balance_history": []
            }

            save_data()
            pending_wallet_input.pop(user_id, None)

            await update.message.reply_text(
                f"✅ Кошелек **{name}** добавлен!\n\n"
                f"🌐 Сеть: {chain.upper()}\n"
                f"📍 {short_addr(address)}\n\n"
                f"🔄 Обновляю баланс...",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )

            # Обновляем баланс
            await update_wallet_balance(user_id, wallet_id)
            return

    # ========== WATCHLIST: ВВОД ПОРОГОВ ==========

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
        "pending_multi": None,
        "multi_params": [],
        "multi_step": 0,
    }


    # ============ ОБРАБОТКА ПОРТФЕЛЯ ============
    if user_id in pending_wallet_input:
        state = pending_wallet_input[user_id]
        
        if text == "Отмена":
            pending_wallet_input.pop(user_id, None)
            await update.message.reply_text("❌ Отмена", reply_markup=main_menu_keyboard())
            return
        
        if state.get("step") == "address":
            # Проверяем адрес
            if len(text) < 30:
                await update.message.reply_text(
                    "❌ Адрес слишком короткий. Проверь и отправь снова.",
                    reply_markup=main_menu_keyboard()
                )
                return
            
            state["address"] = text
            state["step"] = "chain"
            
            # Просим выбрать сеть
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
        
        if state.get("step") == "chain":
            chain_map = {
                "solana": "solana",
                "ethereum": "ethereum",
                "base": "base",
                "bsc": "bsc"
            }
            
            chain = chain_map.get(text.lower())
            if not chain:
                await update.message.reply_text(
                    "❌ Выбери из предложенных вариантов.",
                    reply_markup=main_menu_keyboard()
                )
                return
            
            state["chain"] = chain
            state["step"] = "name"
            
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("Отмена")]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
            
            await update.message.reply_text(
                "📝 Введи название для этого кошелька (например: 'Основной', 'Trading'):",
                reply_markup=keyboard
            )
            return
        
        if state.get("step") == "name":
            # Сохраняем кошелек
            address = state["address"]
            chain = state["chain"]
            name = text if text != "Отмена" else chain.capitalize()
            
            user_data = get_user_wallets(user_id)
            wallet_id = f"wallet_{len(user_data['wallets']) + 1}"
            
            user_data["wallets"][wallet_id] = {
                "address": address,
                "chain": chain,
                "name": name,
                "added_at": int(time.time()),
                "balance": 0,
                "usd_value": 0,
                "balance_history": []
            }
            
            save_data()
            pending_wallet_input.pop(user_id, None)
            
            await update.message.reply_text(
                f"✅ Кошелек **{name}** добавлен!\n\n"
                f"🌐 Сеть: {chain.upper()}\n"
                f"📍 {short_addr(address)}\n\n"
                f"🔄 Обновляю баланс...",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )
            
            # Обновляем баланс сразу
            await update_wallet_balance(user_id, wallet_id)
            return

    # ============ ОБРАБОТКА WATCHLIST ============
    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
        "pending_multi": None,
        "multi_params": [],
        "multi_step": 0,
    }

    # МНОЖЕСТВЕННЫЙ ВВОД ПАРАМЕТРОВ
    if state.get("pending_multi"):
        address = state["pending_multi"]
        multi_params = state.get("multi_params", [])
        multi_step = state.get("multi_step", 0)

        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял число. Введи %, например: 5",
                reply_markup=main_menu_keyboard(),
            )
            return

        if threshold <= 0:
            await update.message.reply_text(
                "❌ Порог должен быть > 0.",
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

        # Заполняем пороги по очереди
        if multi_step == 0 and "price" in multi_params:
            sub["price_threshold"] = threshold
            multi_step = 1
            if "mcap" not in multi_params:
                multi_step = 2
            if "vol" not in multi_params and multi_step == 2:
                multi_step = 3

        elif multi_step == 1 and "mcap" in multi_params:
            sub["mcap_threshold"] = threshold
            multi_step = 2
            if "vol" not in multi_params:
                multi_step = 3

        elif multi_step == 2 and "vol" in multi_params:
            sub["vol_threshold"] = threshold
            multi_step = 3

        state["multi_step"] = multi_step
        pending_threshold_input[user_id] = state

        # Если все параметры введены
        if multi_step >= 3:
            label = format_addr_with_meta(address, info)
            params_text = []
            if sub.get("price_threshold") is not None:
                params_text.append(f"📈 Цена: {sub['price_threshold']:.1f}%")
            if sub.get("mcap_threshold") is not None:
                params_text.append(f"🏦 Капа: {sub['mcap_threshold']:.1f}%")
            if sub.get("vol_threshold") is not None:
                params_text.append(f"🛰 Объём: {sub['vol_threshold']:.1f}%")

            await update.message.reply_text(
                f"✅ Отслеживание для {label} настроено:\n" + "\n".join(params_text),
                reply_markup=main_menu_keyboard(),
            )
            state["pending_multi"] = None
            state["multi_params"] = []
            state["multi_step"] = 0
            pending_threshold_input[user_id] = state
            return

        # Следующий параметр
        next_param = None
        if multi_step == 1 and "mcap" in multi_params:
            next_param = "🏦 капитализации"
        elif multi_step == 2 and "vol" in multi_params:
            next_param = "🛰 объёма m5"

        if next_param:
            await update.message.reply_text(
                f"Введи порог изменения {next_param} в %. Например: 10",
                reply_markup=main_menu_keyboard(),
            )
            return

    # Ввод других параметров watchlist...
    # (остальной код для watchlist остаётся тот же)

    # Если это адрес токена
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
        f"💎 **{symbol}** ({chain_name})\n"
        f"💰 Цена: ${price_cur:,.6f}\n"
        f"🕒 Объём 5m: ${vol_m5_cur:,.0f}\n"
        f"📊 Объём 24ч: ${vol_24h_cur:,.0f}\n"
        f"🏦 Капитализация: ${mcap_cur:,.0f}\n"
        f"🔗 [DexScreener]({pair['url']})"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Цена", callback_data=f"select_price:{address}"),
                InlineKeyboardButton("📊 Капитализация", callback_data=f"select_mcap:{address}"),
            ],
            [
                InlineKeyboardButton("📊 Объем m5", callback_data=f"select_vol:{address}"),
            ],
            [
                InlineKeyboardButton("⚙️ Все параметры", callback_data=f"select_all:{address}"),
            ],
            [
                InlineKeyboardButton("🤖 Спросить ИИ", callback_data=f"askai:{address}"),
            ],
        ]
    )


    await update.message.reply_text(text_resp, reply_markup=keyboard, parse_mode="Markdown")

    
# ------------ КНОПКИ ВЫБОРА ПАРАМЕТРОВ ============

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    data = query.data or ""
    user_id = query.from_user.id

    logger.info(f"BTN от {user_id}: {data}")

    # ============ ВСПЛЫВАЮЩИЕ ПОДСКАЗКИ (TOOLTIPS) ============
    tooltips = {
        "select_all": "✅ Включить все три параметра",
        "select_price": "📈 Отслеживать изменение цены",
        "select_mcap": "🏦 Отслеживать капитализацию",
        "select_vol": "🛰 Отслеживать объём m5",
        "menu": "👁️ Управление параметрами",
        "menu_disabled": "🔄 Активировать параметры",
        "disable_price": "❌ Отключить цену",
        "disable_mcap": "❌ Отключить капу",
        "disable_vol": "❌ Отключить объём",
        "delete": "🗑️ Удалить из списка",
        "pin": "📌 Оставить в списке",
        "back_to_watchlist": "⬅️ Вернуться в Watchlist",
    }

    # Определяем тип кнопки для tooltip
    tooltip_key = None
    for key in tooltips:
        if data.startswith(key):
            tooltip_key = key
            break
    
    if tooltip_key:
        await query.answer(tooltips[tooltip_key], show_alert=False)
    else:
        await query.answer()
    # ==== КНОПКА "СПРОСИТЬ ИИ" ДЛЯ КОНКРЕТНОГО ТОКЕНА ====
    if data.startswith("askai:"):
        address = data.split(":", 1)[1]

        info = tracked_tokens.get(address, {})
        label = format_addr_with_meta(address, info)

        # сохраняем последний выбранный токен и включаем режим "ждём вопрос к ИИ"
        context.user_data["last_token_addr"] = address
        context.user_data["awaiting_ai_question"] = True

        await query.message.reply_text(
            f"🤖 ИИ будет учитывать токен {label}.\n"
            f"Теперь просто напиши свой вопрос (можно без /ai).\n"
            f"Например: `проанализируй этот токен и сравни с моим портфелем`.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return


    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
        "pending_multi": None,
        "multi_params": [],
        "multi_step": 0,
    }

    # ============ ВЫБОР ВСЕХ ТРЁХ ПАРАМЕТРОВ ============
    if data.startswith("select_all:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.setdefault(
            address, {"symbol": None, "chain": None, "subscribers": {}}
        )

        ensure_subscriber(info, user_id)
        state["pending_multi"] = address
        state["multi_params"] = ["price", "mcap", "vol"]
        state["multi_step"] = 0
        pending_threshold_input[user_id] = state

        await query.edit_message_reply_markup(reply_markup=None)
        label = format_addr_with_meta(address, info)
        await query.message.reply_text(
            f"📈 Введи порог изменения цены в % для {label}.\n"
            f"Например: 5",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ============ ВЫБОР ОДНОГО ПАРАМЕТРА ============
    if data.startswith("select_price:"):
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

    if data.startswith("select_mcap:"):
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

    if data.startswith("select_vol:"):
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

        symbol = info.get("symbol", "")
        short_address = short_addr(address)

        text = (
            f"📌 {symbol} {short_address}\n\n"
            f"⛔ Отслеживание отключено\n\n"
            f"Выбери параметры для подключения:"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📈 Цена", callback_data=f"select_price:{address}"
                    ),
                    InlineKeyboardButton(
                        "🏦 Капа", callback_data=f"select_mcap:{address}"
                    ),
                    InlineKeyboardButton(
                        "🛰 Объём", callback_data=f"select_vol:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✅ Все три", callback_data=f"select_all:{address}"
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
        symbol = info.get("symbol", "")
        short_address = short_addr(address)

        vt = sub.get("vol_threshold")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")

        status_lines = [f"📌 **{symbol}** {short_address}"]
        status_lines.append("")
        status_lines.append("**ПАРАМЕТРЫ:**")
        
        if pt is not None:
            status_lines.append(f"✅ 📈 Цена: {pt:.1f}%")
        else:
            status_lines.append(f"⛔ 📈 Цена: отключена")

        if mt is not None:
            status_lines.append(f"✅ 🏦 Капа: {mt:.1f}%")
        else:
            status_lines.append(f"⛔ 🏦 Капа: отключена")

        if vt is not None:
            status_lines.append(f"✅ 🛰 Объём: {vt:.1f}%")
        else:
            status_lines.append(f"⛔ 🛰 Объём: отключен")

        # Анализ памп/дамп
        pump_dump = detect_pump_dump(sub.get("volume_history", deque()))
        if pump_dump:
            status_lines.append("")
            status_lines.append(f"⚡ {pump_dump}")

        text = "\n".join(status_lines)

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

        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
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
            if state.get("pending_multi") == address:
                state["pending_multi"] = None
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


# ============ ФУНКЦИЯ ОБНОВЛЕНИЯ БАЛАНСА ============

async def update_wallet_balance(user_id: int, wallet_id: str):
    """Обновляет баланс кошелька"""
    user_data = get_user_wallets(user_id)
    wallet = user_data["wallets"].get(wallet_id)
    
    if not wallet:
        return
    
    address = wallet["address"]
    chain = wallet["chain"]
    
    if chain == "solana":
        balance_data = await get_solana_balance(address)
    else:
        balance_data = await get_evm_portfolio_moralis(address, chain)
    
    wallet["balance"] = balance_data.get("balance", 0)
    wallet["usd_value"] = balance_data.get("usd_value", 0)
    wallet["last_updated"] = int(time.time())
    
    # Добавляем в историю
    if "balance_history" not in wallet:
        wallet["balance_history"] = []
    
    wallet["balance_history"].append({
        "timestamp": int(time.time()),
        "usd_value": wallet["usd_value"]
    })
    
    # Держим только последние 168 записей (1 неделя при обновлении каждые 10 минут)
    if len(wallet["balance_history"]) > 168:
        wallet["balance_history"] = wallet["balance_history"][-168:]
    
    save_data()


# ============ CALLBACK HANDLER (КНОПКИ) ============

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user_id = query.from_user.id

    logger.info(f"BTN от {user_id}: {data}")

    # если дальше есть tooltips – можно вставить сюда, а затем:
    await query.answer()

    # ============ ПОРТФЕЛЬ CALLBACKS ============

    if data == "portfolio:add":
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await query.message.reply_text(
            "📍 Отправь адрес кошелька (Solana, Ethereum, Base или BSC):",
            reply_markup=keyboard
        )

        pending_wallet_input[user_id] = {"step": "address"}
        return

    if data == "portfolio:view":
        await view_portfolio_full(update, context)
        return

    if data == "portfolio:refresh":
        user_data = get_user_wallets(user_id)
        wallets = user_data.get("wallets", {})

        if not wallets:
            await query.message.reply_text("💼 Портфель пуст!")
            return

        await query.message.reply_text("🔄 Обновляю балансы... (это может занять 30 сек)")

        for wallet_id in wallets:
            await update_wallet_balance(user_id, wallet_id)

        await view_portfolio_full(update, context)
        return

    if data == "portfolio:back":
        await show_portfolio_menu(update, context)
        return

    if data == "portfolio:delete":
        user_data = get_user_wallets(user_id)
        wallets = user_data.get("wallets", {})

        if not wallets:
            await query.message.reply_text("💼 Нет кошельков для удаления!")
            return

        keyboard = []
        for wallet_id, wallet_info in wallets.items():
            name = wallet_info.get("name", "")
            keyboard.append(
                [InlineKeyboardButton(f"🗑️ {name}", callback_data=f"wallet_delete:{wallet_id}")]
            )

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="portfolio:back")])

        await query.edit_message_text(
            text="🗑️ Выбери кошелек для удаления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("wallet_delete:"):
        wallet_id = data.split(":", 1)[1]
        user_data = get_user_wallets(user_id)

        if wallet_id in user_data["wallets"]:
            del user_data["wallets"][wallet_id]
            save_data()
            await query.message.reply_text("✅ Кошелек удален!")

        await show_portfolio_menu(update, context)
        return

    # ============ WATCHLIST CALLBACKS ============

    state = pending_threshold_input.get(user_id) or {
        "pending_volume_for": None,
        "pending_price_for": None,
        "pending_mcap_for": None,
        "pending_multi": None,
        "multi_params": [],
        "multi_step": 0,
    }

    # ВСЕ ТРИ ПАРАМЕТРА
    if data.startswith("select_all:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.setdefault(
            address, {"symbol": None, "chain": None, "subscribers": {}}
        )

        ensure_subscriber(info, user_id)
        state["pending_multi"] = address
        state["multi_params"] = ["price", "mcap", "vol"]
        state["multi_step"] = 0
        pending_threshold_input[user_id] = state

        await query.edit_message_reply_markup(reply_markup=None)
        label = format_addr_with_meta(address, info)
        await query.message.reply_text(
            f"📈 Введи порог изменения цены в % для {label}.\n"
            f"Например: 5",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ОТДЕЛЬНЫЕ ПАРАМЕТРЫ
    if data.startswith("select_price:"):
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

    if data.startswith("select_mcap:"):
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

    if data.startswith("select_vol:"):
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

    # МЕНЮ ОТКЛЮЧЁННОГО ТОКЕНА
    if data.startswith("menu_disabled:"):
        address = data.split(":", 1)[1]
        info = tracked_tokens.get(address)

        if not info or user_id not in info.get("subscribers", {}):
            await query.message.reply_text(
                "⚠️ Этот токен больше не отслеживается.",
                reply_markup=main_menu_keyboard(),
            )
            return

        symbol = info.get("symbol", "")
        short_address = short_addr(address)

        text = (
            f"📌 {symbol} {short_address}\n\n"
            f"⛔ Отслеживание отключено\n\n"
            f"Выбери параметры для подключения:"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📈 Цена", callback_data=f"select_price:{address}"
                    ),
                    InlineKeyboardButton(
                        "🏦 Капа", callback_data=f"select_mcap:{address}"
                    ),
                    InlineKeyboardButton(
                        "🛰 Объём", callback_data=f"select_vol:{address}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✅ Все три", callback_data=f"select_all:{address}"
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

    # МЕНЮ АКТИВНОГО ТОКЕНА
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
        symbol = info.get("symbol", "")
        short_address = short_addr(address)

        vt = sub.get("vol_threshold")
        pt = sub.get("price_threshold")
        mt = sub.get("mcap_threshold")

        status_lines = [f"📌 **{symbol}** {short_address}"]
        status_lines.append("")
        status_lines.append("**ПАРАМЕТРЫ:**")
        
        if pt is not None:
            status_lines.append(f"✅ 📈 Цена: {pt:.1f}%")
        else:
            status_lines.append(f"⛔ 📈 Цена: отключена")

        if mt is not None:
            status_lines.append(f"✅ 🏦 Капа: {mt:.1f}%")
        else:
            status_lines.append(f"⛔ 🏦 Капа: отключена")

        if vt is not None:
            status_lines.append(f"✅ 🛰 Объём: {vt:.1f}%")
        else:
            status_lines.append(f"⛔ 🛰 Объём: отключен")

        pump_dump = detect_pump_dump(sub.get("volume_history", deque()))
        if pump_dump:
            status_lines.append("")
            status_lines.append(f"⚡ {pump_dump}")

        text = "\n".join(status_lines)

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

        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        return

    # СБРОС ПОРОГОВ
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

    # УДАЛЕНИЕ ТОКЕНА
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
            if state.get("pending_multi") == address:
                state["pending_multi"] = None
            pending_threshold_input[user_id] = state

        await query.message.reply_text(
            f"🛑 {label} удален из Watchlist.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # НАЗАД В WATCHLIST
    if data == "back_to_watchlist":
        await watchlist(update, context)
        return

    # ОТКЛЮЧЕНИЕ ПАРАМЕТРОВ ИЗ АЛЕРТА
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


# ============ WATCHLIST ============

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр Watchlist"""
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

    if not items_active and not items_disabled:
        await update.message.reply_text(
            "👀 Сейчас ты ничего не отслеживаешь.",
            reply_markup=main_menu_keyboard(),
        )
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

    text = "🛰 **Твой Watchlist:**\n\nНажми на токен для управления:"
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить токен из watchlist"""
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
        if state.get("pending_multi") == address:
            state["pending_multi"] = None
        pending_threshold_input[user_id] = state

    label = format_addr_with_meta(address, info or {})
    await update.message.reply_text(
        f"✅ Отключил отслеживание для {label}.",
        reply_markup=main_menu_keyboard(),
    )

# ============ ФОНОВЫЙ МОНИТОР ============

def analyze_volume_windows(history: deque, current_ts: float) -> dict:
    """Анализирует объёмы по временным окнам"""
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
    """Фоновый мониторинг токенов и портфеля"""
    logger.info("🚀 Market watcher запущен")

    while True:
        try:
            # ============ ОБНОВЛЕНИЕ WATCHLIST ============
            if tracked_tokens:
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

                        try:
                            trades = pair.get("trades") or {}
                            buy_vol = float(trades.get("h1Buy", 0) or 0)
                            sell_vol = float(trades.get("h1Sell", 0) or 0)
                        except:
                            buy_vol = vol_m5_cur * 0.5
                            sell_vol = vol_m5_cur * 0.5

                        mcap_cur = float(pair.get("marketCap") or pair.get("mcap") or 0)
                        fdv = float(pair.get("fdv") or 0)

                        if not mcap_cur and fdv:
                            mcap_cur = fdv

                        symbol = info.get("symbol") or pair["baseToken"]["symbol"]
                        info["symbol"] = symbol
                        info.setdefault("chain", pair.get("chainId"))

                        for uid, cfg in list(subs.items()):
                            if cfg.get("last_price") is None:
                                cfg["last_price"] = price_cur
                                cfg["last_volume_m5"] = vol_m5_cur
                                cfg["last_mcap"] = mcap_cur
                                cfg["last_ts"] = time.time()
                                cfg["volume_history"].append((time.time(), buy_vol, sell_vol))
                                continue

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

                            vol_windows = analyze_volume_windows(
                                deque([(t, v) for t, _, v in cfg["volume_history"]]), now_ts
                            )

                            extra_lines = []
                            if price_delta is not None:
                                extra_lines.append(f"Цена: {price_delta:+.2f}%")
                            if mcap_delta is not None:
                                extra_lines.append(f"Капитализация: {mcap_delta:+.2f}%")
                            if vol_delta is not None:
                                extra_lines.append(f"Объём m5: {vol_delta:+.2f}%")

                            for window_label, window_change in vol_windows.items():
                                if window_change != 0:
                                    extra_lines.append(f"Объём {window_label}: {window_change:+.1f}%")

                            pump_dump = detect_pump_dump(cfg["volume_history"])

                            label = format_addr_with_meta(address, info)

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

                            cfg["last_price"] = price_cur
                            cfg["last_volume_m5"] = vol_m5_cur
                            cfg["last_mcap"] = mcap_cur
                            cfg["last_ts"] = time.time()

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Критическая ошибка market_watcher: {e}")
            await asyncio.sleep(10)


async def post_init(app: Application):
    logger.info("post_init: запускаем фоновые задачи")
    load_data()
    asyncio.create_task(market_watcher(app))


# ============ MAIN ============

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
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("tools", tools))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(CommandHandler("ai", ai_chat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(ai_callback, pattern="^ai:"))
    app.add_handler(CallbackQueryHandler(button_callback))


    logger.info("🤖 Бот запущен, начинаем polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
