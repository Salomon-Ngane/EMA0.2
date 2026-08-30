"""
=====================================================================
 SIGNAL BOT v0.2 — Bot de trading crypto automatisé (Telegram + Render)
=====================================================================
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta

import pandas as pd
import ta
import ccxt.async_support as ccxt
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from supabase import create_client, Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", 8080))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

CAMEROUN_OFFSET_HEURES = 1  # WAT = UTC+1

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logging.error(f"Erreur d'initialisation Supabase : {e}")
else:
    logging.warning("SUPABASE_URL / SUPABASE_KEY absents — fonctionnement en mode dégradé (mémoire locale uniquement).")

# --- Actifs suivis par défaut ---
DEFAULT_ASSETS = {
    "BTC/USDT": {"enabled": True, "timeframe": "4h"},
    "ETH/USDT": {"enabled": True, "timeframe": "4h"},
    "SOL/USDT": {"enabled": True, "timeframe": "4h"},
    "BNB/USDT": {"enabled": True, "timeframe": "4h"},
    "XRP/USDT": {"enabled": True, "timeframe": "4h"},
}

# --- Persistance globale dans la table "state" ---

def load_all_state():
    default_structure = {"assets": DEFAULT_ASSETS.copy(), "signal_state": {}}
    if not supabase:
        return default_structure
    try:
        response = supabase.table("state").select("data").eq("id", 1).execute()
        if response.data and response.data[0].get("data"):
            data = response.data[0]["data"]
            # Rétrocompatibilité si la structure ancienne contenait seulement les actifs
            if "assets" not in data:
                return {"assets": data, "signal_state": {}}
            return data
    except Exception as e:
        logging.error(f"Erreur chargement Supabase (state) : {e}")
    return default_structure

def save_all_state(state_data):
    if not supabase:
        logging.error("Impossible de sauvegarder : Supabase non configuré.")
        return False
    try:
        supabase.table("state").upsert({"id": 1, "data": state_data}).execute()
        return True
    except Exception as e:
        logging.error(f"Erreur sauvegarde Supabase (state) : {e}")
        return False

# --- État global ---
FULL_STATE = load_all_state()
STATE = FULL_STATE.get("assets", DEFAULT_ASSETS.copy())
SIGNAL_STATE = FULL_STATE.get("signal_state", {})

telegram_app = None
LAST_SCAN_LOGS = {"timestamp": None, "details": [], "signals": []}

# --- Utilitaires ---

def format_dual_time(dt_utc: datetime) -> str:
    heure_cameroun = dt_utc + timedelta(hours=CAMEROUN_OFFSET_HEURES)
    return f"{dt_utc.strftime('%H:%M')} UTC ({heure_cameroun.strftime('%H:%M')} heure du Cameroun)"

# --- Récupération de données (Binance Vision -> Binance Std -> Bybit) ---

EXCHANGE_FALLBACK_ORDER = ["binance_vision", "binance", "bybit"]

def _get_exchange_instance(exchange_id: str):
    if exchange_id == "binance_vision":
        return ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000,
            'options': {'defaultType': 'spot'},
            'urls': {'api': {'public': 'https://data-api.binance.vision/api/v3'}}
        })
    elif exchange_id == "binance":
        return ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000,
            'options': {'defaultType': 'spot'}
        })
    elif exchange_id == "bybit":
        return ccxt.bybit({
            'enableRateLimit': True,
            'timeout': 15000,
            'options': {'defaultType': 'spot'}
        })
    return None

def _is_ip_ban_error(exception) -> bool:
    msg = str(exception)
    return "-1003" in msg or "banned until" in msg.lower() or " 418 " in f" {msg} "

def _format_timeframe_for_exchange(exchange_id: str, timeframe: str) -> str:
    """Convertit le timeframe au format attendu par l'exchange."""
    if exchange_id == "bybit":
        mapping = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "2h": "120",
            "4h": "240",
            "6h": "360",
            "12h": "720",
            "1d": "D",
            "1w": "W",
            "1m": "M"
        }
        return mapping.get(str(timeframe).lower(), str(timeframe))
    return timeframe



async def fetch_ohlcv(symbol, timeframe="4h", limit=100):
    for exchange_id in EXCHANGE_FALLBACK_ORDER:
        exchange = _get_exchange_instance(exchange_id)
        if not exchange:
            continue
        max_attempts = 2
        try:
            ex_timeframe = _format_timeframe_for_exchange(exchange_id, timeframe)
            for attempt in range(max_attempts):
                try:
                    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=ex_timeframe, limit=limit)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    df.attrs['source_exchange'] = exchange_id
                    if exchange_id != EXCHANGE_FALLBACK_ORDER[0]:
                        logging.warning(f"{symbol} : données récupérées via {exchange_id} (secours).")
                    return df
                except Exception as e:
                    if _is_ip_ban_error(e):
                        logging.warning(f"{symbol} : IP bannie sur {exchange_id} — bascule immédiate vers l'exchange suivant.")
                        break
                    logging.error(f"Erreur récupération données pour {symbol} sur {exchange_id} (tentative {attempt+1}/{max_attempts}) : {e}")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(2)
        finally:
            await exchange.close()

    logging.error(f"{symbol} : échec sur tous les exchanges disponibles.")
    return None

async def fetch_top_movers(limit=5, fetch_gainers=True):
    for exchange_id in EXCHANGE_FALLBACK_ORDER:
        exchange = _get_exchange_instance(exchange_id)
        if not exchange:
            continue
        try:
            tickers = await exchange.fetch_tickers()
            usdt_tickers = []
            for symbol, ticker in tickers.items():
                if symbol.endswith('/USDT') and ticker.get('percentage') is not None:
                    usdt_tickers.append({'symbol': symbol, 'change': ticker['percentage'], 'price': ticker['last']})
            usdt_tickers.sort(key=lambda x: x['change'], reverse=fetch_gainers)
            return usdt_tickers[:limit]
        except Exception as e:
            if _is_ip_ban_error(e):
                logging.warning(f"Top movers : IP bannie sur {exchange_id} — bascule immédiate.")
            else:
                logging.error(f"Erreur récupération top movers sur {exchange_id} : {e}")
            continue
        finally:
            await exchange.close()
    return []

# --- Indicateurs & stratégie v0.2 ---

def calculate_indicators(df):
    """EMA 10, MA 35 (RMA), EMA 55."""
    df['ema_10'] = ta.trend.ema_indicator(df['close'], window=10)
    df['ma_35'] = df['close'].ewm(alpha=1/35, adjust=False).mean()
    df['ema_55'] = ta.trend.ema_indicator(df['close'], window=55)
    return df

def find_pivots(data, window=5):
    pivots_low = []
    pivots_high = []
    for i in range(window, len(data) - window):
        current_low = data['low'].iloc[i]
        current_high = data['high'].iloc[i]
        
        if current_low == data['low'].iloc[i-window:i+window+1].min():
            pivots_low.append(current_low)
        if current_high == data['high'].iloc[i-window:i+window+1].max():
            pivots_high.append(current_high)
    return pivots_low, pivots_high

def generate_signal_and_calc_rr(df):
    if len(df) < 65:
        return None, "Données insuffisantes (< 65 bougies)", None, None, None

    curr = df.iloc[-1]
    curr_price = curr['close']

    signal_type = None
    setup_direction = None

    df['ema_above_both'] = (df['ema_10'] > df['ma_35']) & (df['ema_10'] > df['ema_55'])
    df['ema_below_both'] = (df['ema_10'] < df['ma_35']) & (df['ema_10'] < df['ema_55'])

    # 1. Cassure
    breakout_up = df['ema_above_both'].iloc[-1] and not df['ema_above_both'].iloc[-2]
    breakout_down = df['ema_below_both'].iloc[-1] and not df['ema_below_both'].iloc[-2]

    if breakout_up:
        signal_type, setup_direction = "Cassure", "BUY"
    elif breakout_down:
        signal_type, setup_direction = "Cassure", "SELL"

    # 2. Retest (fenêtre 20)
    if not setup_direction:
        lookback = df.iloc[-21:-1].copy()
        breakout_up_idx = lookback.index[(lookback['ema_above_both']) & (~lookback['ema_above_both'].shift(1, fill_value=False))].tolist()
        breakout_down_idx = lookback.index[(lookback['ema_below_both']) & (~lookback['ema_below_both'].shift(1, fill_value=False))].tolist()

        if breakout_up_idx:
            last_idx = breakout_up_idx[-1]
            sub_seq = df.loc[last_idx: df.index[-2]]
            already_retested = any((sub_seq['low'] <= sub_seq['ema_10']) & (sub_seq['close'] > sub_seq['ema_10']))
            curr_retest = (curr['low'] <= curr['ema_10']) and (curr['close'] > curr['ema_10'])
            if curr_retest and not already_retested:
                signal_type, setup_direction = "Retest", "BUY"

        elif breakout_down_idx:
            last_idx = breakout_down_idx[-1]
            sub_seq = df.loc[last_idx: df.index[-2]]
            already_retested = any((sub_seq['high'] >= sub_seq['ema_10']) & (sub_seq['close'] < sub_seq['ema_10']))
            curr_retest = (curr['high'] >= curr['ema_10']) and (curr['close'] < curr['ema_10'])
            if curr_retest and not already_retested:
                signal_type, setup_direction = "Retest", "SELL"

    if not setup_direction:
        return None, "Pas de signal (ni cassure, ni retest)", None, None, None

    # 3. Filtre de tendance (MA 35)
    if setup_direction == "BUY" and curr['close'] <= curr['ma_35']:
        return None, "Rejeté : prix sous la MA 35", None, None, None
    if setup_direction == "SELL" and curr['close'] >= curr['ma_35']:
        return None, "Rejeté : prix au-dessus de la MA 35", None, None, None

    # 4. SL / TP Structurels (v0.2) avec repli v0.1
    pivots_low, pivots_high = find_pivots(df, window=5)

    if setup_direction == "BUY":
        valid_sl_pivots = [p for p in pivots_low if p < curr['ema_10'] and p < curr['ma_35'] and p < curr['ema_55']]
        sl = valid_sl_pivots[-1] if valid_sl_pivots else df.iloc[-10:]['low'].min()
        
        valid_tp_pivots = [p for p in pivots_high if p > curr_price]
        tp = valid_tp_pivots[-1] if valid_tp_pivots else df.iloc[-60:]['high'].max()
        
        risk, reward = curr_price - sl, tp - curr_price
    else:
        valid_sl_pivots = [p for p in pivots_high if p > curr['ema_10'] and p > curr['ma_35'] and p > curr['ema_55']]
        sl = valid_sl_pivots[-1] if valid_sl_pivots else df.iloc[-10:]['high'].max()
        
        valid_tp_pivots = [p for p in pivots_low if p < curr_price]
        tp = valid_tp_pivots[-1] if valid_tp_pivots else df.iloc[-60:]['low'].min()
        
        risk, reward = sl - curr_price, curr_price - tp

    if risk <= 0 or reward <= 0:
        return None, "Configuration invalide : SL/TP incohérent", sl, tp, 0

    rr_ratio = round(reward / risk, 2)

    # 5. Filtre Risk/Reward >= 2.7
    if rr_ratio < 2.7:
        return None, f"Rejeté : R:R = {rr_ratio} < 2.7", sl, tp, rr_ratio

    return setup_direction, f"Valide ({signal_type}) — R:R = {rr_ratio}", sl, tp, rr_ratio

# --- Scan principal ---

async def run_scan_job():
    global LAST_SCAN_LOGS, STATE, SIGNAL_STATE
    scan_time = datetime.utcnow()
    logging.info("Lancement de l'analyse des marchés...")

    LAST_SCAN_LOGS = {
        "timestamp": scan_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "details": [],
        "signals": []
    }

    new_signals = []

    for symbol, config in list(STATE.items()):
        if not config.get("enabled", True):
            continue

        timeframe = config.get("timeframe", "4h")
        try:
            df = await fetch_ohlcv(symbol, timeframe=timeframe)
            await asyncio.sleep(1.0)

            if df is None or df.empty:
                LAST_SCAN_LOGS["details"].append(f"❌ **{symbol}** : données indisponibles.")
                continue

            df = calculate_indicators(df)
            direction, reason, sl, tp, rr = generate_signal_and_calc_rr(df)

            curr = df.iloc[-1]
            source = df.attrs.get('source_exchange', 'bybit')
            source_note = "" if source == "binance_vision" else f" _(via {source}, secours)_"
            LAST_SCAN_LOGS["details"].append(f"**{symbol}** : {curr['close']} | {reason}{source_note}")

            if direction:
                bar_time = curr['timestamp'].isoformat()
                previous = SIGNAL_STATE.get(symbol, {})
                is_duplicate = previous.get("bar_time") == bar_time and previous.get("direction") == direction

                if not is_duplicate:
                    signal_type = "Cassure" if "Cassure" in reason else "Retest"
                    new_signals.append({
                        "symbol": symbol, "direction": direction, "signal_type": signal_type,
                        "price": curr['close'], "sl": sl, "tp": tp, "rr": rr, "timeframe": timeframe
                    })
                    SIGNAL_STATE[symbol] = {"bar_time": bar_time, "direction": direction}
                else:
                    LAST_SCAN_LOGS["details"][-1] += " (déjà notifié précédemment)"

        except Exception as e:
            logging.error(f"Erreur lors du traitement de {symbol} : {e}")
            LAST_SCAN_LOGS["details"].append(f"❌ **{symbol}** : {e}")
            continue

    save_all_state({"assets": STATE, "signal_state": SIGNAL_STATE})

    if not (telegram_app and TELEGRAM_CHAT_ID):
        return

    for sig in new_signals:
        emoji_dir = "📈" if sig["direction"] == "BUY" else "📉"
        message = (
            f"🚨 **Nouveau signal détecté** {emoji_dir}\n\n"
            f"**Actif :** {sig['symbol']}\n"
            f"**Direction :** {sig['direction']}\n"
            f"**Type :** {sig['signal_type']}\n"
            f"**Prix d'entrée :** {sig['price']}\n"
            f"🛑 **Stop-Loss :** {sig['sl']:.4f}\n"
            f"🎯 **Take-Profit :** {sig['tp']:.4f}\n"
            f"⚖️ **Risk/Reward :** {sig['rr']}\n"
            f"⏱ **Timeframe :** {sig['timeframe']}"
        )
        try:
            await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Erreur envoi Telegram (signal) : {e}")

    LAST_SCAN_LOGS["signals"] = [f"{s['symbol']} {s['direction']} ({s['signal_type']})" for s in new_signals]
    heure_affichage = format_dual_time(scan_time)
    nb_actifs = len([s for s in STATE.values() if s.get("enabled", True)])

    if new_signals:
        recap = (
            f"✅ **Analyse terminée** — {heure_affichage}\n\n"
            f"🎯 {len(new_signals)} signal(s) détecté(s) sur {nb_actifs} actif(s) surveillé(s).\n"
            f"Le détail est ci-dessus. Tapez /logs pour la synthèse complète."
        )
    else:
        recap = (
            f"✅ **Analyse terminée** — {heure_affichage}\n\n"
            f"📭 Aucune opportunité ne remplit nos critères pour le moment sur les {nb_actifs} actif(s) surveillé(s).\n"
            f"Prochaine analyse automatique dans 4 heures. Tapez /logs pour le détail par actif."
        )
    try:
        await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=recap, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Erreur envoi Telegram (récapitulatif) : {e}")

# --- Commandes Telegram ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bonjour ! Je suis votre assistant de trading automatisé v0.2.\n\n"
        "Je surveille les marchés crypto en continu (00h, 04h, 08h, 12h, 16h, 20h UTC).\n\n"
        "📋 **Commandes disponibles :**\n"
        "/scan — lancer une analyse manuelle\n"
        "/list — voir les actifs suivis\n"
        "/add_asset — ajouter un actif à la liste\n"
        "/remove_asset — retirer un actif\n"
        "/gainers — top 5 hausses (24h)\n"
        "/losers — top 5 baisses (24h)\n"
        "/logs — détail de la dernière analyse",
        parse_mode="Markdown"
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STATE:
        await update.message.reply_text("📭 Aucun actif suivi pour le moment.")
        return
    msg = "📋 **Actifs actuellement suivis :**\n\n"
    for symbol, cfg in STATE.items():
        status = "✅" if cfg.get("enabled", True) else "⏸️"
        msg += f"{status} **{symbol}** — timeframe {cfg.get('timeframe', '4h')}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("ℹ️ Usage : `/add_asset SYMBOLE [timeframe]`\nExemple : `/add_asset SOL/USDT 4h`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    tf = args[1] if len(args) > 1 else "4h"
    STATE[symbol] = {"enabled": True, "timeframe": tf}
    if save_all_state({"assets": STATE, "signal_state": SIGNAL_STATE}):
        await update.message.reply_text(f"✅ **{symbol}** ajouté à la liste (timeframe {tf}).", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ **{symbol}** ajouté localement, mais échec sauvegarde Supabase.", parse_mode="Markdown")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("ℹ️ Usage : `/remove_asset SYMBOLE`\nExemple : `/remove_asset BTC/USDT`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    if symbol in STATE:
        del STATE[symbol]
        if save_all_state({"assets": STATE, "signal_state": SIGNAL_STATE}):
            await update.message.reply_text(f"🗑️ **{symbol}** retiré de la liste de suivi.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ **{symbol}** retiré localement, mais échec sauvegarde Supabase.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❓ **{symbol}** ne fait pas partie de la liste actuelle.", parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analyse manuelle en cours, merci de patienter...")
    await run_scan_job()

async def gainers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Récupération du Top 5 Gainers (24h)...")
    movers = await fetch_top_movers(limit=5, fetch_gainers=True)
    if not movers:
        await update.message.reply_text("⚠️ Impossible de récupérer les données pour le moment.")
        return
    msg = "🔥 **Top 5 hausses (24h) :**\n\n"
    for item in movers:
        msg += f"🟢 **{item['symbol']}** : +{item['change']:.2f}% — {item['price']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def losers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📉 Récupération du Top 5 Losers (24h)...")
    movers = await fetch_top_movers(limit=5, fetch_gainers=False)
    if not movers:
        await update.message.reply_text("⚠️ Impossible de récupérer les données pour le moment.")
        return
    msg = "🔻 **Top 5 baisses (24h) :**\n\n"
    for item in movers:
        msg += f"🔴 **{item['symbol']}** : {item['change']:.2f}% — {item['price']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LAST_SCAN_LOGS["timestamp"]:
        await update.message.reply_text("📭 Aucune analyse n'a encore été effectuée.")
        return
    msg = f"📊 **Détail de la dernière analyse** — {LAST_SCAN_LOGS['timestamp']}\n\n"
    msg += "🚨 **Signaux détectés :**\n"
    msg += ("\n".join(f"• {s}" for s in LAST_SCAN_LOGS["signals"]) if LAST_SCAN_LOGS["signals"] else "Aucun.")
    msg += "\n\n🔎 **Détail par actif :**\n"
    msg += "\n".join(f"• {d}" for d in LAST_SCAN_LOGS["details"])
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- Serveur HTTP ---

async def handle_health(request):
    return web.Response(text="Bot actif ✅", status=200)

async def handle_scan_request(request):
    asyncio.create_task(run_scan_job())
    return web.Response(text="Analyse déclenchée avec succès.", status=200)

# --- Point d'entrée ---

async def main():
    global telegram_app

    server = web.Application()
    server.router.add_get('/health', handle_health)
    server.router.add_get('/', handle_health)
    server.router.add_get('/scan', handle_scan_request)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Serveur HTTP démarré sur le port {PORT}.")

    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN manquant.")
        await asyncio.Event().wait()
        return

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("scan", scan_cmd))
    telegram_app.add_handler(CommandHandler("list", list_cmd))
    telegram_app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    telegram_app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    telegram_app.add_handler(CommandHandler("gainers", gainers_cmd))
    telegram_app.add_handler(CommandHandler("losers", losers_cmd))
    telegram_app.add_handler(CommandHandler("logs", logs_cmd))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logging.info("Bot Telegram v0.2 prêt et à l'écoute.")

    asyncio.create_task(run_scan_job())
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
