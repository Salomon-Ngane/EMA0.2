import asyncio
import json
import websockets
import pandas as pd

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

TF_MAP = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800
}

def _to_dataframe(candles) -> pd.DataFrame:
    formatted = []
    for c in candles:
        formatted.append({
            'timestamp': c.get('epoch', 0) * 1000,
            'open': float(c.get('open', 0)),
            'high': float(c.get('high', 0)),
            'low': float(c.get('low', 0)),
            'close': float(c.get('close', 0)),
            'volume': float(c.get('volume', 0))
        })
    df = pd.DataFrame(formatted)
    df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['SMA35'] = df['close'].rolling(window=35).mean()
    df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
    return df

async def fetch_ohlcv(symbol: str, timeframe: str = "4h", limit: int = 150) -> pd.DataFrame:
    granularity = TF_MAP.get(timeframe, 14400)
    payload = {
        "ticks_history": symbol.upper(),
        "style": "candles",
        "granularity": granularity,
        "count": limit,
        "end": "latest"
    }
    
    async with websockets.connect(DERIV_WS_URL) as websocket:
        await websocket.send(json.dumps(payload))
        response = await websocket.recv()
        data = json.loads(response)
        
        if "error" in data:
            raise RuntimeError(f"Erreur Deriv pour {symbol}: {data['error'].get('message')}")
        
        candles = data.get("candles", [])
        if not candles:
            raise RuntimeError(f"Aucune bougie reçue pour {symbol} sur Deriv")
            
        return _to_dataframe(candles)
