"""
=====================================================================
 SIGNAL BOT v0.4 — Bot Multi-Utilisateurs, Master List & Backtest
=====================================================================
"""

import os
import json
import io
import html
import asyncio
import logging
from datetime import datetime, timedelta

import pandas as pd
import ta
import ccxt.async_support as ccxt
import websockets
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from supabase import create_client, Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
ADMIN_USERNAME = "@ideasanddreams"
CAMEROUN_OFFSET_HEURES = 1

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logging.error(f"Erreur initialisation Supabase : {e}")
else:
    logging.warning("Supabase non configuré. Mode dégradé local actif.")

# --- Gestion Base de Données & Whitelist ---

def get_user_profile(telegram_id: int):
    """Récupère le profil d'un utilisateur (role, max_assets)."""
    if not supabase:
        return {"role": "admin", "max_assets": 999}
    try:
        res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        logging.error(f"Erreur get_user_profile : {e}")
    return None

def is_whitelisted(telegram_id: int) -> bool:
    return get_user_profile(telegram_id) is not None

def is_admin(telegram_id: int) -> bool:
    profile = get_user_profile(telegram_id)
    return profile is not None and profile.get("role") == "admin"

def load_global_state():
    """
    Structure stockée :
    {
      "user_assets": { "USER_ID": { "BTC/USDT": {"enabled": True, "timeframe": "4h"} } },
      "signal_state": { "USER_ID_BTC/USDT": {"bar_time": "...", "direction": "BUY"} }
    }
    """
    default_struct = {"user_assets": {}, "signal_state": {}}
    if not supabase:
        return default_struct
    try:
        res = supabase.table("state").select("data").eq("id", 1).execute()
        if res.data and res.data[0].get("data"):
            return res.data[0]["data"]
    except Exception as e:
        logging.error(f"Erreur chargement state : {e}")
    return default_struct

def save_global_state(state_data):
    if not supabase:
        return False
    try:
        supabase.table("state").upsert({"id": 1, "data": state_data}).execute()
        return True
    except Exception as e:
        logging.error(f"Erreur sauvegarde state : {e}")
        return False

# Variables Globales
GLOBAL_DATA = load_global_state()
USER_ASSETS = GLOBAL_DATA.get("user_assets", {})
SIGNAL_STATE = GLOBAL_DATA.get("signal_state", {})

telegram_app = None
LAST_SCAN_LOGS = {"timestamp": None, "details": [], "signals": []}

# --- Utilitaires ---

def format_dual_time(dt_utc: datetime) -> str:
    heure_cam = dt_utc + timedelta(hours=CAMEROUN_OFFSET_HEURES)
    return f"{dt_utc.strftime('%H:%M')} UTC ({heure_cam.strftime('%H:%M')} Cameroun)"

def is_deriv_symbol(symbol: str) -> bool:
    sym = symbol.upper()
    return sym.startswith("R_") or sym.startswith("1HZ") or "VOLATILITY" in sym or sym.startswith("HZ")

def build_master_list():
    """Génère la Master List des actifs uniques à scanner et leurs timeframes."""
    master = {}
    for uid, assets in USER_ASSETS.items():
        for sym, cfg in assets.items():
            if cfg.get("enabled", True):
                tf = cfg.get("timeframe", "4h")
                if sym not in master:
                    master[sym] = set()
                master[sym].add(tf)
    return master

# --- Data Fetching (Deriv & CCXT) ---

async def fetch_deriv_ohlcv(symbol: str, timeframe: str = "4h", count: int = 200) -> pd.DataFrame:
    granularity_map = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "1d": 86400
    }
    granularity = granularity_map.get(str(timeframe).lower(), 14400)
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    req = {
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": count, "end": "latest", "style": "candles", "granularity": granularity
    }
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps(req))
            res = json.loads(await ws.recv())
            if "error" in res or "candles" not in res:
                return None
            df = pd.DataFrame(res["candles"])
            df = df.rename(columns={"epoch": "timestamp", "open": "open", "high": "high", "low": "low", "close": "close"})
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.attrs['source_exchange'] = 'deriv'
            return df
    except Exception as e:
        logging.error(f"Erreur Deriv WS ({symbol}) : {e}")
        return None

EXCHANGE_FALLBACK = ["binance_vision", "binance", "bybit"]

def _get_exchange_instance(ex_id: str):
    if ex_id == "binance_vision":
        return ccxt.binance({'enableRateLimit': True, 'timeout': 15000, 'urls': {'api': {'public': 'https://data-api.binance.vision/api/v3'}}})
    elif ex_id == "binance":
        return ccxt.binance({'enableRateLimit': True, 'timeout': 15000})
    elif ex_id == "bybit":
        return ccxt.bybit({'enableRateLimit': True, 'timeout': 15000})
    return None

async def fetch_ohlcv(symbol: str, timeframe: str = "4h", limit: int = 200):
    if is_deriv_symbol(symbol):
        return await fetch_deriv_ohlcv(symbol, timeframe, count=limit)
    for ex_id in EXCHANGE_FALLBACK:
        ex = _get_exchange_instance(ex_id)
        if not ex: continue
        try:
            ex_tf = timeframe if ex_id != "bybit" else {"1m":"1","5m":"5","15m":"15","1h":"60","4h":"240","1d":"D"}.get(timeframe.lower(), timeframe)
            ohlcv = await ex.fetch_ohlcv(symbol, timeframe=ex_tf, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.attrs['source_exchange'] = ex_id
            return df
        except Exception:
            continue
        finally:
            await ex.close()
    return None

async def fetch_top_movers(limit=5):
    gainers, losers = [], []
    for ex_id in EXCHANGE_FALLBACK:
        ex = _get_exchange_instance(ex_id)
        if not ex: continue
        try:
            tickers = await ex.fetch_tickers()
            usdt = [{'symbol': s, 'change': t['percentage'], 'price': t['last']} for s, t in tickers.items() if s.endswith('/USDT') and t.get('percentage') is not None]
            usdt.sort(key=lambda x: x['change'], reverse=True)
            gainers = usdt[:limit]
            losers = usdt[-limit:]
            losers.reverse()
            return gainers, losers
        except Exception:
            continue
        finally:
            await ex.close()
    return gainers, losers

# --- Stratégie & Moteur de Backtest ---

def calculate_indicators(df):
    df['ema_10'] = ta.trend.ema_indicator(df['close'], window=10)
    df['ma_35'] = df['close'].ewm(alpha=1/35, adjust=False).mean()
    df['ema_55'] = ta.trend.ema_indicator(df['close'], window=55)
    return df

def find_pivots(data, window=5):
    p_low, p_high = [], []
    for i in range(window, len(data) - window):
        c_low, c_high = data['low'].iloc[i], data['high'].iloc[i]
        if c_low == data['low'].iloc[i-window:i+window+1].min(): p_low.append(c_low)
        if c_high == data['high'].iloc[i-window:i+window+1].max(): p_high.append(c_high)
    return p_low, p_high

def evaluate_candle(df, idx):
    """Évalue la bougie à l'index `idx` pour détecter un signal."""
    if idx < 65: return None, "Données insuffisantes", None, None, None
    sub_df = df.iloc[:idx+1]
    curr = sub_df.iloc[-1]
    curr_price = curr['close']

    ema_above = (sub_df['ema_10'] > sub_df['ma_35']) & (sub_df['ema_10'] > sub_df['ema_55'])
    ema_below = (sub_df['ema_10'] < sub_df['ma_35']) & (sub_df['ema_10'] < sub_df['ema_55'])

    breakout_up = ema_above.iloc[-1] and not ema_above.iloc[-2]
    breakout_down = ema_below.iloc[-1] and not ema_below.iloc[-2]

    sig_type, setup_dir = None, None
    if breakout_up: sig_type, setup_dir = "Cassure", "BUY"
    elif breakout_down: sig_type, setup_dir = "Cassure", "SELL"

    if not setup_dir:
        lookback = sub_df.iloc[-21:-1]
        b_up_idx = lookback.index[(ema_above.loc[lookback.index]) & (~ema_above.loc[lookback.index].shift(1, fill_value=False))].tolist()
        b_down_idx = lookback.index[(ema_below.loc[lookback.index]) & (~ema_below.loc[lookback.index].shift(1, fill_value=False))].tolist()

        if b_up_idx:
            sub_seq = sub_df.loc[b_up_idx[-1]: sub_df.index[-2]]
            if (curr['low'] <= curr['ema_10'] and curr['close'] > curr['ema_10']) and not any((sub_seq['low'] <= sub_seq['ema_10']) & (sub_seq['close'] > sub_seq['ema_10'])):
                sig_type, setup_dir = "Retest", "BUY"
        elif b_down_idx:
            sub_seq = sub_df.loc[b_down_idx[-1]: sub_df.index[-2]]
            if (curr['high'] >= curr['ema_10'] and curr['close'] < curr['ema_10']) and not any((sub_seq['high'] >= sub_seq['ema_10']) & (sub_seq['close'] < sub_seq['ema_10'])):
                sig_type, setup_dir = "Retest", "SELL"

    if not setup_dir: return None, "Pas de signal", None, None, None
    if setup_dir == "BUY" and curr['close'] <= curr['ma_35']: return None, "Filtre MA35", None, None, None
    if setup_dir == "SELL" and curr['close'] >= curr['ma_35']: return None, "Filtre MA35", None, None, None

    p_low, p_high = find_pivots(sub_df, window=5)
    if setup_dir == "BUY":
        v_sl = [p for p in p_low if p < curr['ema_10'] and p < curr['ma_35'] and p < curr['ema_55']]
        sl = v_sl[-1] if v_sl else sub_df.iloc[-10:]['low'].min()
        v_tp = [p for p in p_high if p > curr_price]
        tp = v_tp[-1] if v_tp else sub_df.iloc[-60:]['high'].max()
        risk, reward = curr_price - sl, tp - curr_price
    else:
        v_sl = [p for p in p_high if p > curr['ema_10'] and p > curr['ma_35'] and p > curr['ema_55']]
        sl = v_sl[-1] if v_sl else sub_df.iloc[-10:]['high'].max()
        v_tp = [p for p in p_low if p < curr_price]
        tp = v_tp[-1] if v_tp else sub_df.iloc[-60:]['low'].min()
        risk, reward = sl - curr_price, curr_price - tp

    if risk <= 0 or reward <= 0: return None, "Invalide SL/TP", sl, tp, 0
    rr = round(reward / risk, 2)
    if rr < 2.7: return None, f"R:R faible ({rr})", sl, tp, rr

    return setup_dir, f"Valide ({sig_type})", sl, tp, rr

def run_backtest_logic(df, days: int):
    """Exécute le backtest complet sur un DataFrame."""
    df = calculate_indicators(df)
    cutoff = datetime.utcnow() - timedelta(days=days)
    df_filtered = df[df['timestamp'] >= cutoff]
    
    signals = []
    if len(df_filtered) == 0: return signals

    start_idx = df.index.get_loc(df_filtered.index[0])
    for i in range(start_idx, len(df)):
        direction, reason, sl, tp, rr = evaluate_candle(df, i)
        if direction:
            row = df.iloc[i]
            signals.append({
                "timestamp": row['timestamp'].strftime("%Y-%m-%d %H:%M UTC"),
                "direction": direction, "price": row['close'],
                "sl": sl, "tp": tp, "rr": rr, "reason": reason
            })
    return signals

# --- Task de Scan Périodique ---

async def run_scan_job():
    global LAST_SCAN_LOGS, USER_ASSETS, SIGNAL_STATE
    scan_time = datetime.utcnow()
    logging.info("Démarrage du scan Master List...")

    master_list = build_master_list()
    LAST_SCAN_LOGS = {"timestamp": scan_time.strftime("%Y-%m-%d %H:%M:%S UTC"), "details": [], "signals": []}

    fetched_cache = {}
    for symbol, tfs in master_list.items():
        for tf in tfs:
            df = await fetch_ohlcv(symbol, timeframe=tf)
            if df is not None and not df.empty:
                df = calculate_indicators(df)
                fetched_cache[(symbol, tf)] = df
            await asyncio.sleep(0.3)

    for uid_str, assets in USER_ASSETS.items():
        uid = int(uid_str)
        user_signals = []

        for symbol, cfg in assets.items():
            if not cfg.get("enabled", True): continue
            tf = cfg.get("timeframe", "4h")
            df = fetched_cache.get((symbol, tf))

            if df is None:
                LAST_SCAN_LOGS["details"].append(f"❌ <b>{symbol} [{tf}]</b> : Données indisponibles.")
                continue

            direction, reason, sl, tp, rr = evaluate_candle(df, len(df) - 1)
            curr = df.iloc[-1]
            safe_reason = html.escape(str(reason))
            LAST_SCAN_LOGS["details"].append(f"<b>{symbol} [{tf}]</b> : {curr['close']} | {safe_reason}")

            if direction:
                bar_time = curr['timestamp'].isoformat()
                sig_key = f"{uid}_{symbol}_{tf}"
                prev = SIGNAL_STATE.get(sig_key, {})

                if prev.get("bar_time") != bar_time or prev.get("direction") != direction:
                    user_signals.append({
                        "symbol": symbol, "direction": direction, "price": curr['close'],
                        "sl": sl, "tp": tp, "rr": rr, "timeframe": tf, "type": "Cassure" if "Cassure" in reason else "Retest"
                    })
                    SIGNAL_STATE[sig_key] = {"bar_time": bar_time, "direction": direction}

        if user_signals and telegram_app:
            for sig in user_signals:
                emoji = "📈" if sig["direction"] == "BUY" else "📉"
                msg = (
                    f"🚨 <b>Signal Détecté [{sig['timeframe']}]</b> {emoji}\n\n"
                    f"<b>Actif :</b> {sig['symbol']}\n"
                    f"<b>Direction :</b> {sig['direction']} ({sig['type']})\n"
                    f"<b>Entrée :</b> {sig['price']}\n"
                    f"🛑 <b>SL :</b> {sig['sl']:.4f}\n"
                    f"🎯 <b>TP :</b> {sig['tp']:.4f}\n"
                    f"⚖️ <b>R:R :</b> {sig['rr']}"
                )
                try:
                    await telegram_app.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Erreur envoi signal à {uid} : {e}")

    save_global_state({"user_assets": USER_ASSETS, "signal_state": SIGNAL_STATE})

# --- Commandes Telegram ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text(f"⛔ <b>Accès restreint.</b>\nVous n'êtes pas sur la liste blanche. Contactez l'admin : {ADMIN_USERNAME}", parse_mode="HTML")
        return

    prof = get_user_profile(uid)
    await update.message.reply_text(
        f"👋 <b>Bienvenue dans le Bot Signal v0.4</b>\n\n"
        f"👤 <b>Statut :</b> {prof['role'].upper()}\n"
        f"📊 <b>Limite d'actifs :</b> {prof['max_assets']}\n\n"
        "📋 <b>Commandes :</b>\n"
        "/list — Voir vos actifs\n"
        "/add_asset <symbole> [tf] — Ajouter un actif\n"
        "/remove_asset <symbole> — Retirer un actif\n"
        "/set_tf <tf> <symbole1> <symbole2>... — Modifier le timeframe\n"
        "/scan — Lancer une analyse manuelle\n\n"
        "🛠 <b>Commandes Admin :</b>\n"
        "/backtest <symbole> <jours> [tf]\n"
        "/top_scan [jours]\n"
        "/allow <user_id> [role]",
        parse_mode="HTML"
    )

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin : Ajouter un utilisateur à la Whitelist."""
    if not is_admin(update.effective_user.id): return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage : <code>/allow <user_id> [free/premium/admin]</code>", parse_mode="HTML")
        return
    target_id = int(args[0])
    role = args[1] if len(args) > 1 else "free"
    max_a = 999 if role == "admin" else (15 if role == "premium" else 3)
    
    if supabase:
        supabase.table("users").upsert({"telegram_id": target_id, "role": role, "max_assets": max_a}).execute()
        await update.message.reply_text(f"✅ Utilisateur <code>{target_id}</code> ajouté (Rôle: {role}, Limite: {max_a}).", parse_mode="HTML")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    assets = USER_ASSETS.get(uid, {})
    if not assets:
        await update.message.reply_text("📭 Votre liste d'actifs est vide.", parse_mode="HTML")
        return
    prof = get_user_profile(int(uid))
    msg = f"📋 <b>Vos actifs suivis ({len(assets)}/{prof['max_assets']}) :</b>\n\n"
    for sym, cfg in assets.items():
        msg += f"• <b>{sym}</b> — Timeframe: <code>{cfg.get('timeframe', '4h')}</code>\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_num = update.effective_user.id
    if not is_whitelisted(uid_num): return
    uid = str(uid_num)
    args = context.args

    if len(args) < 1:
        await update.message.reply_text("Usage : <code>/add_asset <SYMBOLE> [timeframe]</code>", parse_mode="HTML")
        return

    sym = args[0].upper()
    tf = args[1].lower() if len(args) > 1 else "4h"
    prof = get_user_profile(uid_num)
    u_assets = USER_ASSETS.get(uid, {})

    if len(u_assets) >= prof['max_assets'] and sym not in u_assets:
        await update.message.reply_text(
            f"⚠️ <b>Limite d'actifs atteinte ({prof['max_assets']} max).</b>\n\n"
            f"Pour débloquer le statut Premium et ajouter plus d'actifs, contactez l'administrateur : {ADMIN_USERNAME}",
            parse_mode="HTML"
        )
        return

    if uid not in USER_ASSETS: USER_ASSETS[uid] = {}
    USER_ASSETS[uid][sym] = {"enabled": True, "timeframe": tf}
    save_global_state({"user_assets": USER_ASSETS, "signal_state": SIGNAL_STATE})
    await update.message.reply_text(f"✅ <b>{sym}</b> ajouté à votre liste d'analyse (Timeframe: <code>{tf}</code>).", parse_mode="HTML")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage : <code>/remove_asset <SYMBOLE></code>", parse_mode="HTML")
        return
    sym = args[0].upper()
    if uid in USER_ASSETS and sym in USER_ASSETS[uid]:
        del USER_ASSETS[uid][sym]
        save_global_state({"user_assets": USER_ASSETS, "signal_state": SIGNAL_STATE})
        await update.message.reply_text(f"🗑️ <b>{sym}</b> retiré de votre liste.", parse_mode="HTML")
    else:
        await update.message.reply_text("❓ Actif introuvable dans votre liste.", parse_mode="HTML")

async def set_tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Modifie le timeframe de plusieurs actifs d'un coup."""
    uid = str(update.effective_user.id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage : <code>/set_tf <timeframe> <symbole1> <symbole2>...</code>\nEx: <code>/set_tf 1h BTC/USDT ETH/USDT</code>", parse_mode="HTML")
        return
    tf = args[0].lower()
    symbols = [s.upper() for s in args[1:]]
    if uid not in USER_ASSETS:
        await update.message.reply_text("📭 Votre liste d'actifs est vide.", parse_mode="HTML")
        return

    updated = []
    for sym in symbols:
        if sym in USER_ASSETS[uid]:
            USER_ASSETS[uid][sym]["timeframe"] = tf
            updated.append(sym)

    if updated:
        save_global_state({"user_assets": USER_ASSETS, "signal_state": SIGNAL_STATE})
        await update.message.reply_text(f"✅ Timeframe ajusté à <code>{tf}</code> pour : {', '.join(updated)}", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Aucun actif correspondant n'a été trouvé dans votre liste.", parse_mode="HTML")

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin : Génère un fichier .txt de backtest."""
    if not is_admin(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage : <code>/backtest <SYMBOLE> <JOURS> [timeframe]</code>", parse_mode="HTML")
        return

    sym = args[0].upper()
    days = int(args[1])
    tf = args[2].lower() if len(args) > 2 else "4h"

    await update.message.reply_text(f"⏳ Calcul du backtest pour <b>{sym} [{tf}]</b> sur {days} jours...", parse_mode="HTML")

    df = await fetch_ohlcv(sym, timeframe=tf, limit=1000)
    if df is None or df.empty:
        await update.message.reply_text("❌ Impossible de récupérer les données historiques.", parse_mode="HTML")
        return

    signals = run_backtest_logic(df, days)

    # Création du fichier .txt
    output = io.StringIO()
    output.write(f"=== RAPPORT DE BACKTEST : {sym} [{tf}] ===\n")
    output.write(f"Période : {days} derniers jours | Généré le : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n")
    output.write(f"Total signaux détectés : {len(signals)}\n\n")

    for idx, sig in enumerate(signals, 1):
        output.write(f"#{idx} [{sig['timestamp']}] | {sig['direction']} | Entrée: {sig['price']} | SL: {sig['sl']:.4f} | TP: {sig['tp']:.4f} | R:R: {sig['rr']}\n")

    file_bytes = io.BytesIO(output.getvalue().encode('utf-8'))
    file_bytes.name = f"backtest_{sym.replace('/', '_')}_{tf}_{days}d.txt"

    await update.message.reply_document(
        document=file_bytes,
        caption=f"📊 <b>Backtest {sym} [{tf}]</b> — {len(signals)} signal(s) trouvé(s) sur {days} jours.",
        parse_mode="HTML"
    )

async def top_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin : Scan Express Gainers/Losers + Backtest + Boutons."""
    if not is_admin(update.effective_user.id): return
    args = context.args
    days = int(args[0]) if args else 7

    await update.message.reply_text(f"⚡ Analyse et backtest des Top Gainers/Losers (Backtest : {days} jours)...", parse_mode="HTML")

    gainers, losers = await fetch_top_movers(limit=5)
    keyboard = []

    msg = f"🔥 <b>Top Scan Express & Backtest ({days}j)</b>\n\n"
    msg += "🟢 <b>TOP 5 GAINERS :</b>\n"

    for g in gainers:
        sym = g['symbol']
        df = await fetch_ohlcv(sym, timeframe="4h", limit=500)
        sig_count = len(run_backtest_logic(df, days)) if df is not None else 0
        msg += f"• <b>{sym}</b> (+{g['change']:.1f}%) : {sig_count} signal(s)\n"
        keyboard.append([InlineKeyboardButton(f"➕ Ajouter {sym}", callback_data=f"add_{sym}")])

    msg += "\n🔴 <b>TOP 5 LOSERS :</b>\n"
    for l in losers:
        sym = l['symbol']
        df = await fetch_ohlcv(sym, timeframe="4h", limit=500)
        sig_count = len(run_backtest_logic(df, days)) if df is not None else 0
        msg += f"• <b>{sym}</b> ({l['change']:.1f}%) : {sig_count} signal(s)\n"
        keyboard.append([InlineKeyboardButton(f"➕ Ajouter {sym}", callback_data=f"add_{sym}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestion des clics sur les boutons interactifs du top_scan."""
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = str(query.from_user.id)
    uid_num = query.from_user.id

    if data.startswith("add_"):
        sym = data.replace("add_", "")
        prof = get_user_profile(uid_num)
        u_assets = USER_ASSETS.get(uid, {})

        if len(u_assets) >= prof['max_assets'] and sym not in u_assets:
            await query.message.reply_text(f"⚠️ Limite d'actifs atteinte ({prof['max_assets']} max). Contactez {ADMIN_USERNAME} pour passer Premium.")
            return

        if uid not in USER_ASSETS: USER_ASSETS[uid] = {}
        USER_ASSETS[uid][sym] = {"enabled": True, "timeframe": "4h"}
        save_global_state({"user_assets": USER_ASSETS, "signal_state": SIGNAL_STATE})
        await query.message.reply_text(f"✅ <b>{sym}</b> ajouté à votre liste de suivi !", parse_mode="HTML")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Lancement du scan de votre liste...", parse_mode="HTML")
    await run_scan_job()

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LAST_SCAN_LOGS["timestamp"]:
        await update.message.reply_text("📭 Aucun scan enregistré.", parse_mode="HTML")
        return
    msg = f"📊 <b>Détail du dernier scan</b> — {LAST_SCAN_LOGS['timestamp']}\n\n"
    msg += "\n".join(f"• {d}" for d in LAST_SCAN_LOGS["details"])
    await update.message.reply_text(msg, parse_mode="HTML")

# --- Serveur HTTP & Initialisation ---

async def handle_health(request): return web.Response(text="Bot actif ✅", status=200)

async def main():
    global telegram_app
    server = web.Application()
    server.router.add_get('/', handle_health)
    server.router.add_get('/health', handle_health)

    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN manquant.")
        return

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("allow", allow_cmd))
    telegram_app.add_handler(CommandHandler("list", list_cmd))
    telegram_app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    telegram_app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    telegram_app.add_handler(CommandHandler("set_tf", set_tf_cmd))
    telegram_app.add_handler(CommandHandler("backtest", backtest_cmd))
    telegram_app.add_handler(CommandHandler("top_scan", top_scan_cmd))
    telegram_app.add_handler(CommandHandler("scan", scan_cmd))
    telegram_app.add_handler(CommandHandler("logs", logs_cmd))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logging.info("Bot Telegram v0.4 démarré.")

    asyncio.create_task(run_scan_job())
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

