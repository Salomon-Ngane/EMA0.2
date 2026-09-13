from services.exchanges import crypto, deriv

def is_deriv_symbol(symbol: str) -> bool:
    s = symbol.upper()
    return any(x in s for x in ["VOL", "R_", "BOOM", "CRASH", "1HZ"])

def get_fetcher_for(symbol: str):
    if is_deriv_symbol(symbol):
        return deriv.fetch_ohlcv
    return crypto.fetch_ohlcv

async def fetch_ohlcv_routed(symbol: str, timeframe: str = "4h", limit: int = 150):
    fetcher = get_fetcher_for(symbol)
    return await fetcher(symbol, timeframe, limit)
