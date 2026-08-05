"""
Signal Bot - MA25/MA55 Cross Strategy (Cassure + Retest) -> Telegram
=====================================================================
Version optimisée avec notification de démarrage et réglages souples.
"""

import os
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd

# =====================================================================
# CONFIGURATION - Ajustée pour recevoir des signaux réguliers
# =====================================================================

# Si la variable SYMBOLS est définie dans GitHub, on la découpe par virgule.
# Sinon, on utilise la liste par défaut.
DEFAULT_SYMBOLS = "BTCUSDT,BNBUSDT,SUIUSDT,ADAUSDT,XRPUSDT"
ENV_SYMBOLS = os.environ.get("SYMBOLS", DEFAULT_SYMBOLS)
SYMBOLS = [s.strip().upper() for s in ENV_SYMBOLS.split(",") if s.strip()]

SIGNAL_TIMEFRAME = "2h"
HTF_TIMEFRAME = "4h"

LEN_FAST = 10   # EMA verte
LEN_TREND = 35  # MA35
LEN_SLOW = 55   # EMA55

RETEST_WINDOW = 20
LOOKBACK_4H = 5

USE_TREND_FILTER = True
USE_RR_FILTER = True
MIN_RR = 1.5   # NIVEAU AJUSTE (2.7 était trop strict)

SIGNAL_SOURCE = "both"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
# NOUVELLE LIGNE :
BINANCE_KLINES_URL = "https://api1.binance.com/api/v3/klines"



# =====================================================================
# FONCTIONS TELEGRAM & SERVICES
# =====================================================================

def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.ok:
            return True
        else:
            print(f"[!] Échec envoi Telegram ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        print(f"[!] Erreur de connexion Telegram: {e}")
        return False


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
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

    now = pd.Timestamp.now(tz="UTC")
    if df.iloc[-1]["close_time"] > now:
        df = df.iloc[:-1].reset_index(drop=True)

    return df


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma25"] = ema(df["close"], LEN_FAST)
    df["ma35"] = rma(df["close"], LEN_TREND)
    df["ma55"] = ema(df["close"], LEN_SLOW)
    return df


def compute_htf_levels(df_4h: pd.DataFrame) -> pd.DataFrame:
    df_4h = df_4h.copy()
    df_4h["ph4h"] = df_4h["high"].rolling(LOOKBACK_4H).max()
    df_4h["pl4h"] = df_4h["low"].rolling(LOOKBACK_4H).min()
    return df_4h[["close_time", "ph4h", "pl4h"]]


def merge_htf(df_signal: pd.DataFrame, df_4h_levels: pd.DataFrame) -> pd.DataFrame:
    df_signal = df_signal.sort_values("close_time")
    df_4h_levels = df_4h_levels.sort_values("close_time")
    return pd.merge_asof(df_signal, df_4h_levels, on="close_time", direction="backward")


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    ema_above_both = (df["ma25"] > df["ma35"]) & (df["ma25"] > df["ma55"])
    ema_below_both = (df["ma25"] < df["ma35"]) & (df["ma25"] < df["ma55"])

    df["breakout_up"] = ema_above_both & ~ema_above_both.shift(1, fill_value=False)
    df["breakout_down"] = ema_below_both & ~ema_below_both.shift(1, fill_value=False)

    retest_long = [False] * len(df)
    retest_short = [False] * len(df)
    waiting_long, waiting_short = False, False
    breakout_bar_long, breakout_bar_short = None, None

    lows, highs, closes, ma25v = df["low"].values, df["high"].values, df["close"].values, df["ma25"].values

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


def format_message(symbol, signal_type, direction, row) -> str:
    emoji = "🟢" if direction == "LONG" else "🔴"
    rr = row["rr_long"] if direction == "LONG" else row["rr_short"]
    return (
        f"{emoji} <b>{signal_type.upper()} {direction}</b> — {symbol} ({SIGNAL_TIMEFRAME})\n"
        f"Prix : {row['close']:.6g}\n"
        f"TP : {row['ph4h'] if direction == 'LONG' else row['pl4h']:.6g}\n"
        f"SL : {row['pl4h'] if direction == 'LONG' else row['ph4h']:.6g}\n"
        f"R:R : {rr:.2f}\n"
        f"Bougie clôturée : {row['close_time'].strftime('%Y-%m-%d %H:%M UTC')}"
    )


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def process_symbol(symbol: str, state: dict) -> int:
    df_signal = fetch_klines(symbol, SIGNAL_TIMEFRAME, limit=300)
    df_4h = fetch_klines(symbol, HTF_TIMEFRAME, limit=max(50, LOOKBACK_4H + 10))

    df_signal = compute_indicators(df_signal)
    htf_levels = compute_htf_levels(df_4h)
    df_signal = merge_htf(df_signal, htf_levels)
    df_signal = compute_signals(df_signal)

    signals_sent = 0
    checks = [
        ("cassure", "LONG", "breakout_long_signal"),
        ("cassure", "SHORT", "breakout_short_signal"),
        ("retest", "LONG", "retest_long_signal"),
        ("retest", "SHORT", "retest_short_signal"),
    ]

    # Analyse des 2 dernières bougies clôturées pour ne rater aucun signal
    for idx in [-2, -1]:
        row = df_signal.iloc[idx]
        last_ts = row["close_time"].isoformat()

        for signal_type, direction, col in checks:
            if not bool(row[col]):
                continue
            key = f"{symbol}:{signal_type}:{direction}"
            if state.get(key) == last_ts:
                continue
            
            msg = format_message(symbol, signal_type, direction, row)
            if send_telegram_message(msg):
                state[key] = last_ts
                signals_sent += 1
                print(f"[OK] Alerte envoyée : {key} @ {last_ts}")

    return signals_sent


def main():
    print("🚀 Démarrage du Signal Bot...")
    
    # Message d'accueil / test de santé Telegram
    init_sent = send_telegram_message("🤖 <b>Signal Bot actif</b> — Analyse des paires en cours...")
    if not init_sent:
        print("[!] Impossible d'envoyer le message de test sur Telegram. Vérifiez votre TOKEN et CHAT_ID.")
    
    state = load_state()
    total_signals = 0

    for symbol in SYMBOLS:
        try:
            total_signals += process_symbol(symbol, state)
        except Exception as e:
            print(f"[ERREUR] {symbol}: {e}")
        time.sleep(0.3)

    save_state(state)
    print(f"✅ Analyse terminée. {total_signals} signal(aux) envoyé(s).")


if __name__ == "__main__":
    main()

