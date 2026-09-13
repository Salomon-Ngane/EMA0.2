import asyncio
import pandas as pd
import ccxt.async_support as ccxt

EXCHANGE_CLASSES = {
    "binance": ccxt.binance,
    "bybit": ccxt.bybit
}
EXCHANGE_FALLBACK_ORDER = ["binance", "bybit"]

def _is_ip_ban_error(exception) -> bool:
    msg = str(exception)
    return "-1003" in msg or "banned until" in msg.lower() or " 418 " in f" {msg} "

def _to_dataframe(ohlcv) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['SMA35'] = df['close'].rolling(window=35).mean()
    df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
    return df

async def fetch_ohlcv(symbol: str, timeframe: str = "4h", limit: int = 150) -> pd.DataFrame:
    for exchange_id in EXCHANGE_FALLBACK_ORDER:
        exchange_cls = EXCHANGE_CLASSES[exchange_id]
        exchange = exchange_cls({'enableRateLimit': True, 'timeout': 15000})
        try:
            for attempt in range(2):
                try:
                    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                    return _to_dataframe(ohlcv)
                except Exception as e:
                    if _is_ip_ban_error(e):
                        break  # Pas de 2e tentative sur un exchange banni
                    if attempt == 0:
                        await asyncio.sleep(2)
        finally:
            await exchange.close()
    raise RuntimeError(f"{symbol} indisponible sur {', '.join(EXCHANGE_FALLBACK_ORDER)}")
