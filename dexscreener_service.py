import aiohttp
import logging

logger = logging.getLogger(__name__)

DEXSCREENER_API_URL = "https://api.dexscreener.com"


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    try:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.warning(f"DexScreener request error: {e} for {url}")
        return None


async def get_token_pairs_by_address(session: aiohttp.ClientSession, address: str):
    """
    Аналог dexscreener_token_info/getTokenPairs из плагина.
    Берём все пары по адресу токена.
    """
    url = f"{DEXSCREENER_API_URL}/latest/dex/tokens/{address}"
    return await fetch_json(session, url)


async def get_trending_pairs(session: aiohttp.ClientSession, timeframe: str = "6h", limit: int = 10):
    """
    Аналог dexscreener_trending.
    timeframe: 1h / 6h / 24h
    """
    url = f"{DEXSCREENER_API_URL}/latest/dex/trending"
    params = {"timeframe": timeframe, "limit": limit}
    return await fetch_json(session, url)


async def get_new_pairs(session: aiohttp.ClientSession, chain: str | None = None, limit: int = 10):
    """
    Аналог dexscreener_new_pairs.
    """
    url = f"{DEXSCREENER_API_URL}/latest/dex/pairs"
    params = {"limit": limit}
    if chain:
        params["chain"] = chain
    return await fetch_json(session, url)


def pick_best_pair(data: dict | None):
    """
    Выбирает одну «лучшую» пару из ответа DexScreener,
    примерно как это делает плагин: по ликвидности/объёму.
    """
    if not data or "pairs" not in data or not data["pairs"]:
        return None

    pairs = data["pairs"]
    # сортируем по ликвидности и объёму за 24ч
    def score(p):
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        vol = (p.get("volume") or {}).get("h24", 0) or 0
        return liq * 2 + vol

    pairs_sorted = sorted(pairs, key=score, reverse=True)
    return pairs_sorted[0]
