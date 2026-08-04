"""
Signal Bot - MA25/MA55 Cross Strategy (Cassure + Retest) -> Telegram
=====================================================================

Reproduit la logique du script Pine v6 "MA25/MA55 Cross Strategy" :
  - ma25 = EMA(close, LEN_FAST)         -> ligne verte rapide (defaut 10)
  - ma35 = RMA(close, LEN_TREND)        -> filtre de tendance (defaut 35)
  - ma55 = EMA(close, LEN_SLOW)         -> ligne jaune (defaut 55)

  Signal 1 (Cassure)  : ma25 sort au-dessus/en-dessous de ma35 ET ma55
  Signal 2 (Retest)   : le prix revient toucher ma25 puis cloture du bon
                         cote, dans une fenetre de RETEST_WINDOW bougies
                         apres la cassure

  Filtres :
    - Tendance (MA35)   : close > ma35 pour un long, < pour un short
    - R:R minimum       : calcule avec le plus haut / plus bas des
                           LOOKBACK_4H dernieres bougies 4H (comme
                           ta.highest/ta.lowest dans le script Pine)

Aucune dependance a TradingView : les donnees sont recuperees
directement depuis l'API publique de Binance (pas de cle API requise
pour les prix).

Le script est concu pour tourner periodiquement (cron / GitHub Actions).
Il garde un etat local (state.json) pour ne jamais renvoyer deux fois
la meme alerte.
"""

import os
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd

# =====================================================================
# CONFIGURATION - a adapter selon tes besoins
# =====================================================================

# Symboles Binance a surveiller (format API Binance, sans tiret)
SYMBOLS = ["BTCUSDT", "BNBUSDT", "SUIUSDT", "ADAUSDT", "XRPUSDT"]

# Timeframe sur lequel les signaux Cassure/Retest sont detectes
# Valeurs Binance valides : 1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d ...
SIGNAL_TIMEFRAME = "30m"

# Timeframe utilise pour le TP/SL et le filtre R:R (comme le script Pine)
HTF_TIMEFRAME = "4h"

# Longueurs des moyennes mobiles (identiques aux inputs du script Pine)
LEN_FAST = 10   # EMA verte (ma25)
LEN_TREND = 35  # MA35 (RMA / lissee)
LEN_SLOW = 55   # EMA55 (jaune)

# Fenetre max (en bougies du timeframe signal) pour attendre un retest
# apres une cassure
RETEST_WINDOW = 2

# Fenetre glissante sur le 4H pour le plus haut / plus bas (TP/SL)
LOOKBACK_4H = 5

# Filtres actifs (memes noms que dans le script Pine)
USE_TREND_FILTER = True
USE_RR_FILTER = True
MIN_RR = 2.7

# Quel(s) signal(aux) doivent generer une alerte : "cassure", "retest", "both"
SIGNAL_SOURCE = "both"

# Telegram (a definir en variables d'environnement / secrets, jamais en dur)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fichier d'etat pour eviter les doublons d'alerte entre deux executions
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


# =====================================================================
# RECUPERATION DES DONNEES
# =====================================================================

def fetch_klines(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """Recupere les bougies closes depuis l'API publique Binance."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    # On ecarte la derniere bougie si elle n'est pas encore cloturee
    now = pd.Timestamp.now(tz="UTC")
    if df.iloc[-1]["close_time"] > now:
        df = df.iloc[:-1].reset_index(drop=True)

    return df


# =====================================================================
# INDICATEURS (equivalents Pine)
# =====================================================================

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Equivalent de ta.rma() dans Pine (lissage de Wilder, alpha = 1/length)."""
    return series.ewm(alpha=1 / length, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma25"] = ema(df["close"], LEN_FAST)
    df["ma35"] = rma(df["close"], LEN_TREND)
    df["ma55"] = ema(df["close"], LEN_SLOW)
    return df


def compute_htf_levels(df_4h: pd.DataFrame) -> pd.DataFrame:
    """Plus haut / plus bas glissant sur LOOKBACK_4H bougies 4H (TP/SL)."""
    df_4h = df_4h.copy()
    df_4h["ph4h"] = df_4h["high"].rolling(LOOKBACK_4H).max()
    df_4h["pl4h"] = df_4h["low"].rolling(LOOKBACK_4H).min()
    return df_4h[["close_time", "ph4h", "pl4h"]]


def merge_htf(df_signal: pd.DataFrame, df_4h_levels: pd.DataFrame) -> pd.DataFrame:
    """Associe a chaque bougie du timeframe signal le dernier ph4h/pl4h
    connu au moment ou cette bougie s'est cloturee (equivalent de
    request.security avec lookahead_off)."""
    df_signal = df_signal.sort_values("close_time")
    df_4h_levels = df_4h_levels.sort_values("close_time")
    merged = pd.merge_asof(
        df_signal, df_4h_levels,
        on="close_time", direction="backward",
    )
    return merged


# =====================================================================
# LOGIQUE DE SIGNAL (Cassure + Retest)
# =====================================================================

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    ema_above_both = (df["ma25"] > df["ma35"]) & (df["ma25"] > df["ma55"])
    ema_below_both = (df["ma25"] < df["ma35"]) & (df["ma25"] < df["ma55"])

    df["breakout_up"] = ema_above_both & ~ema_above_both.shift(1, fill_value=False)
    df["breakout_down"] = ema_below_both & ~ema_below_both.shift(1, fill_value=False)

    # --- Retest (boucle sequentielle, comme le var/if du script Pine) ---
    retest_long = [False] * len(df)
    retest_short = [False] * len(df)
    waiting_long = False
    waiting_short = False
    breakout_bar_long = None
    breakout_bar_short = None

    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values
    ma25v = df["ma25"].values

    for i in range(len(df)):
        if df["breakout_up"].iloc[i]:
            waiting_long, waiting_short = True, False
            breakout_bar_long = i
        if df["breakout_down"].iloc[i]:
            waiting_short, waiting_long = True, False
            breakout_bar_short = i

        if waiting_long and breakout_bar_long is not None and (i - breakout_bar_long > RETEST_WINDOW):
            waiting_long = False
        if waiting_short and breakout_bar_short is not None and (i - breakout_bar_short > RETEST_WINDOW):
            waiting_short = False

        if waiting_long and lows[i] <= ma25v[i] and closes[i] > ma25v[i]:
            retest_long[i] = True
            waiting_long = False
        if waiting_short and highs[i] >= ma25v[i] and closes[i] < ma25v[i]:
            retest_short[i] = True
            waiting_short = False

    df["retest_long"] = retest_long
    df["retest_short"] = retest_short

    # --- Filtres ---
    trend_up = df["close"] > df["ma35"]
    trend_down = df["close"] < df["ma35"]

    risk_long = df["close"] - df["pl4h"]
    reward_long = df["ph4h"] - df["close"]
    rr_long = reward_long / risk_long
    rr_ok_long = (risk_long > 0) & (rr_long >= MIN_RR)

    risk_short = df["ph4h"] - df["close"]
    reward_short = df["close"] - df["pl4h"]
    rr_short = reward_short / risk_short
    rr_ok_short = (risk_short > 0) & (rr_short >= MIN_RR)

    def gate(raw, trend_ok, rr_ok):
        out = raw.copy()
        if USE_TREND_FILTER:
            out &= trend_ok
        if USE_RR_FILTER:
            out &= rr_ok
        return out

    use_breakout = SIGNAL_SOURCE in ("cassure", "both")
    use_retest = SIGNAL_SOURCE in ("retest", "both")

    df["breakout_long_signal"] = gate(df["breakout_up"] & use_breakout, trend_up, rr_ok_long)
    df["breakout_short_signal"] = gate(df["breakout_down"] & use_breakout, trend_down, rr_ok_short)
    df["retest_long_signal"] = gate(df["retest_long"] & use_retest, trend_up, rr_ok_long)
    df["retest_short_signal"] = gate(df["retest_short"] & use_retest, trend_down, rr_ok_short)

    df["rr_long"] = rr_long
    df["rr_short"] = rr_short

    return df


# =====================================================================
# TELEGRAM
# =====================================================================

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant - message non envoye.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"[!] Echec envoi Telegram ({resp.status_code}): {resp.text}")


def format_message(symbol, signal_type, direction, row) -> str:
    emoji = "🟢" if direction == "LONG" else "🔴"
    rr = row["rr_long"] if direction == "LONG" else row["rr_short"]
    return (
        f"{emoji} <b>{signal_type.upper()} {direction}</b> — {symbol} ({SIGNAL_TIMEFRAME})\n"
        f"Prix : {row['close']:.6g}\n"
        f"TP : {row['ph4h'] if direction == 'LONG' else row['pl4h']:.6g}\n"
        f"SL : {row['pl4h'] if direction == 'LONG' else row['ph4h']:.6g}\n"
        f"R:R : {rr:.2f}\n"
        f"Bougie cloturee : {row['close_time'].strftime('%Y-%m-%d %H:%M UTC')}"
    )


# =====================================================================
# ETAT (anti-doublon)
# =====================================================================

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# =====================================================================
# BOUCLE PRINCIPALE
# =====================================================================

def process_symbol(symbol: str, state: dict) -> None:
    df_signal = fetch_klines(symbol, SIGNAL_TIMEFRAME, limit=300)
    df_4h = fetch_klines(symbol, HTF_TIMEFRAME, limit=max(50, LOOKBACK_4H + 10))

    df_signal = compute_indicators(df_signal)
    htf_levels = compute_htf_levels(df_4h)
    df_signal = merge_htf(df_signal, htf_levels)
    df_signal = compute_signals(df_signal)

    last = df_signal.iloc[-1]
    last_ts = last["close_time"].isoformat()

    checks = [
        ("cassure", "LONG", "breakout_long_signal"),
        ("cassure", "SHORT", "breakout_short_signal"),
        ("retest", "LONG", "retest_long_signal"),
        ("retest", "SHORT", "retest_short_signal"),
    ]

    for signal_type, direction, col in checks:
        if not bool(last[col]):
            continue
        key = f"{symbol}:{signal_type}:{direction}"
        if state.get(key) == last_ts:
            continue  # deja alerte pour cette bougie
        msg = format_message(symbol, signal_type, direction, last)
        send_telegram_message(msg)
        state[key] = last_ts
        print(f"[OK] Alerte envoyee : {key} @ {last_ts}")


def main():
    state = load_state()
    for symbol in SYMBOLS:
        try:
            process_symbol(symbol, state)
        except Exception as e:
            print(f"[ERREUR] {symbol}: {e}")
        time.sleep(0.3)  # petite pause pour rester sous les limites Binance
    save_state(state)

import requests, os

# Test rapide d'envoi
token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")
requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": "🔔 Test réussi depuis GitHub Actions !"})

if __name__ == "__main__":
    main()
