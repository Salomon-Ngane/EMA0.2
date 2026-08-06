"""
Signal Bot - MA25/MA55 Cross Strategy (Cassure + Retest) -> Telegram
=====================================================================
Version optimisée 24/7 pour Render (Flask Health Check + Boucle continue)
"""

import os
import json
import time
import hashlib
import threading
from datetime import datetime, timezone

import requests
import pandas as pd
from flask import Flask, request

# =====================================================================
# CONFIGURATION - Ajustée pour recevoir des signaux réguliers
# =====================================================================

DEFAULT_SYMBOLS = "BTCUSDT,BNBUSDT,SUIUSDT,ADAUSDT,XRPUSDT"
ENV_SYMBOLS = os.environ.get("SYMBOLS", DEFAULT_SYMBOLS)
SYMBOLS = [s.strip().upper() for s in ENV_SYMBOLS.split(",") if s.strip()]

SIGNAL_TIMEFRAME = "4h"
HTF_TIMEFRAME = "4h"

LEN_FAST = 10   # EMA verte
LEN_TREND = 35  # MA35
LEN_SLOW = 55   # EMA55

RETEST_WINDOW = 20
LOOKBACK_4H = 5

USE_TREND_FILTER = True
USE_RR_FILTER = True
MIN_RR = 2.7   

SIGNAL_SOURCE = "both"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
BINANCE_KLINES_URL = "https://api1.binance.com/api/v3/klines"

# Secret de sécurité pour l'URL du webhook Telegram (évite qu'un tiers
# devine l'URL et poste de faux updates). On peut le fixer via variable
# d'env, sinon il est dérivé du token du bot.
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:16] if TELEGRAM_BOT_TOKEN else "no-token"
)

# Cache en mémoire : dernier snapshot d'indicateurs calculé par symbole,
# rempli à chaque run (auto ou /scan), consulté par /signals.
LAST_DIAGNOSTIC: dict = {}


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
    df["trend_up"] = trend_up
    df["trend_down"] = trend_down
    df["rr_ok_long"] = rr_ok_long
    df["rr_ok_short"] = rr_ok_short

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

    # --- Diagnostic : toujours visible dans les logs, meme si aucun signal
    # n'est envoye. Permet de verifier ce que le bot a reellement calcule.
    last_row = df_signal.iloc[-1]
    diag = {
        "close_time": last_row["close_time"].strftime("%Y-%m-%d %H:%M UTC"),
        "close": float(last_row["close"]),
        "ma25": float(last_row["ma25"]),
        "ma35": float(last_row["ma35"]),
        "ma55": float(last_row["ma55"]),
        "breakout_up": bool(last_row["breakout_up"]),
        "breakout_down": bool(last_row["breakout_down"]),
        "retest_long": bool(last_row["retest_long"]),
        "retest_short": bool(last_row["retest_short"]),
        "trend_up": bool(last_row["trend_up"]),
        "rr_long": float(last_row["rr_long"]) if pd.notna(last_row["rr_long"]) else float("nan"),
        "rr_ok_long": bool(last_row["rr_ok_long"]),
        "rr_short": float(last_row["rr_short"]) if pd.notna(last_row["rr_short"]) else float("nan"),
        "rr_ok_short": bool(last_row["rr_ok_short"]),
    }
    LAST_DIAGNOSTIC[symbol] = diag
    print(
        f"[DIAG] {symbol} @ {diag['close_time']} | "
        f"close={diag['close']:.6g} ma25={diag['ma25']:.6g} "
        f"ma35={diag['ma35']:.6g} ma55={diag['ma55']:.6g} | "
        f"breakout_up={diag['breakout_up']} breakout_down={diag['breakout_down']} "
        f"retest_long={diag['retest_long']} retest_short={diag['retest_short']} | "
        f"trend_up={diag['trend_up']} "
        f"rr_long={diag['rr_long']:.2f} rr_ok_long={diag['rr_ok_long']} "
        f"rr_short={diag['rr_short']:.2f} rr_ok_short={diag['rr_ok_short']}"
    )

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
    print("🔍 Analyse du marché en cours...")
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


# =====================================================================
# COMMANDES TELEGRAM (/scan, /signals, /add, /remove)
# =====================================================================

def run_scan_and_notify() -> None:
    """Lance une analyse immédiate de tous les symboles suivis (déclenché par /scan)."""
    send_telegram_message("🔍 Scan manuel en cours...")
    state = load_state()
    total = 0
    for symbol in list(SYMBOLS):
        try:
            total += process_symbol(symbol, state)
        except Exception as e:
            print(f"[ERREUR scan manuel] {symbol}: {e}")
        time.sleep(0.3)
    save_state(state)
    send_telegram_message(
        f"✅ Scan terminé. {total} signal(aux) envoyé(s). Tape /signals pour le détail des indicateurs."
    )


def format_signals_snapshot() -> str:
    if not LAST_DIAGNOSTIC:
        return "Aucune donnée pour le moment — attends le premier run ou tape /scan."
    lines = ["📊 <b>Dernier snapshot par symbole</b>"]
    for symbol, d in LAST_DIAGNOSTIC.items():
        lines.append(
            f"\n<b>{symbol}</b> @ {d['close_time']}\n"
            f"Prix: {d['close']:.6g} | MA25:{d['ma25']:.6g} MA35:{d['ma35']:.6g} MA55:{d['ma55']:.6g}\n"
            f"Breakout ↑{d['breakout_up']} ↓{d['breakout_down']} | "
            f"Retest ↑{d['retest_long']} ↓{d['retest_short']}\n"
            f"Tendance haussière: {d['trend_up']} | "
            f"RR long: {d['rr_long']:.2f} (ok={d['rr_ok_long']}) — "
            f"RR short: {d['rr_short']:.2f} (ok={d['rr_ok_short']})"
        )
    return "\n".join(lines)


def handle_add(symbol_raw: str) -> None:
    global SYMBOLS
    symbol = symbol_raw.strip().upper()
    if not symbol:
        send_telegram_message("Usage : /add BTCUSDT")
        return
    if symbol in SYMBOLS:
        send_telegram_message(f"{symbol} est déjà suivi.")
        return
    try:
        fetch_klines(symbol, SIGNAL_TIMEFRAME, limit=5)
    except Exception:
        send_telegram_message(f"❌ Impossible de valider {symbol} sur Binance (symbole invalide ?).")
        return
    SYMBOLS.append(symbol)
    send_telegram_message(f"✅ {symbol} ajouté. Liste suivie ({len(SYMBOLS)}) : {', '.join(SYMBOLS)}")


def handle_remove(symbol_raw: str) -> None:
    symbol = symbol_raw.strip().upper()
    if symbol not in SYMBOLS:
        send_telegram_message(f"{symbol} n'est pas dans la liste suivie.")
        return
    SYMBOLS.remove(symbol)
    LAST_DIAGNOSTIC.pop(symbol, None)
    reste = ', '.join(SYMBOLS) if SYMBOLS else "(vide)"
    send_telegram_message(f"🗑 {symbol} retiré. Liste actuelle : {reste}")


def handle_command(text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]  # tolère "/scan@monbot"
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/scan":
        threading.Thread(target=run_scan_and_notify, daemon=True).start()
    elif cmd == "/signals":
        send_telegram_message(format_signals_snapshot())
    elif cmd == "/add":
        handle_add(arg)
    elif cmd == "/remove":
        handle_remove(arg)
    elif cmd == "/start" or cmd == "/help":
        send_telegram_message(
            "Commandes disponibles :\n"
            "/scan — lance une analyse immédiate\n"
            "/signals — dernier snapshot d'indicateurs par symbole (même filtré)\n"
            "/add SYMBOLE — ajoute un symbole (ex: /add DOGEUSDT)\n"
            "/remove SYMBOLE — retire un symbole"
        )
    else:
        send_telegram_message("Commande inconnue. Tape /help pour la liste.")


def register_webhook() -> None:
    """Indique à Telegram où envoyer les updates (nécessite un Web Service
    Render avec URL publique — pas un Background Worker)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        print("[!] RENDER_EXTERNAL_URL introuvable — webhook non configuré. "
              "Vérifie que le service Render est bien un 'Web Service' (pas un Background Worker).")
        return
    webhook_url = f"{base_url}/telegram/{WEBHOOK_SECRET}"
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            params={"url": webhook_url},
            timeout=15,
        )
        print(f"[Webhook] setWebhook -> {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[!] Erreur configuration webhook: {e}")


# =====================================================================
# CONFIGURATION SERVEUR RENDER & BOUCLE CONTINU 24/7
# =====================================================================

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive!", 200


@app.route(f"/telegram/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text:
        return "ok", 200
    if not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
        # Ignore silencieusement tout message venant d'un autre chat.
        print(f"[!] Commande ignorée depuis chat_id non autorisé: {chat_id}")
        return "ok", 200

    try:
        handle_command(text)
    except Exception as e:
        print(f"[ERREUR commande] {e}")
        send_telegram_message(f"⚠️ Erreur en traitant la commande : {e}")

    return "ok", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def main_loop():
    print("🤖 Bot démarré sur serveur 24/7...")
    send_telegram_message("🚀 Bot d'analyse démarré en continu sur Render (24/7) !")
    
    while True:
        try:
            main()
        except Exception as e:
            print(f"[!] Erreur durant l'exécution : {e}")
        
        print("💤 En attente du prochain intervalle (1 heure)...")
        time.sleep(3600)

if __name__ == "__main__":
    # Démarre le serveur web en arrière-plan
    threading.Thread(target=run_flask, daemon=True).start()
    # Laisse le temps au serveur Flask de bind le port avant d'enregistrer le webhook
    time.sleep(2)
    register_webhook()
    # Démarre la boucle infinie d'analyse
    main_loop()
