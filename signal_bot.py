import os
import asyncio
import logging
from datetime import datetime, timedelta
from aiohttp import web
import ccxt.async_support as ccxt
import pandas as pd
import ta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from supabase import create_client, Client

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Tokens & Configs
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", 8080))

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logging.error(f"Erreur d'initialisation Supabase : {e}")

# Default Assets Configuration (Sans leverage)
DEFAULT_ASSETS = {
    "BTC/USDT": {"enabled": True, "timeframe": "4h"},
    "ETH/USDT": {"enabled": True, "timeframe": "4h"},
    "SOL/USDT": {"enabled": True, "timeframe": "4h"},
    "BNB/USDT": {"enabled": True, "timeframe": "4h"},
    "XRP/USDT": {"enabled": True, "timeframe": "4h"},
}

# Mémoire globale pour stocker les logs du dernier scan
LAST_SCAN_LOGS = {
    "timestamp": None,
    "details": [],
    "signals": []
}

def load_state():
    if not supabase:
        logging.warning("Supabase non connecté. Utilisation de la liste locale par défaut.")
        return DEFAULT_ASSETS.copy()
    
    try:
        response = supabase.table("state").select("data").eq("id", 1).execute()
        if response.data and len(response.data) > 0 and response.data[0]["data"]:
            return response.data[0]["data"]
    except Exception as e:
        logging.error(f"Erreur chargement Supabase : {e}")
    
    return DEFAULT_ASSETS.copy()

def save_state(state):
    if not supabase:
        logging.error("Impossible de sauvegarder : Supabase non configuré.")
        return False
        
    try:
        supabase.table("state").update({"data": state}).eq("id", 1).execute()
        return True
    except Exception as e:
        logging.error(f"Erreur sauvegarde Supabase : {e}")
        return False

STATE = load_state()
telegram_app = None

async def fetch_ohlcv(symbol, timeframe="4h", limit=100):
    valid_timeframes = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
    if not timeframe or timeframe not in valid_timeframes:
        timeframe = "4h"

    exchange = ccxt.binance({
        'enableRateLimit': True, 
        'rateLimit': 2000,
        'timeout': 10000,
        'options': {'defaultType': 'spot'}
    })
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.error(f"Erreur récupération données pour {symbol}: {e}")
        return None
    finally:
        await exchange.close()

async def fetch_top_movers(limit=5, fetch_gainers=True):
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'rateLimit': 2000,
        'timeout': 10000
    })
    try:
        tickers = await exchange.fetch_tickers()
        usdt_tickers = []
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and ticker.get('percentage') is not None:
                usdt_tickers.append({
                    'symbol': symbol,
                    'change': ticker['percentage'],
                    'price': ticker['last']
                })
        
        usdt_tickers.sort(key=lambda x: x['change'], reverse=fetch_gainers)
        return usdt_tickers[:limit]
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des movers 24h : {e}")
        return []
    finally:
        await exchange.close()

def calculate_indicators(df):
    """Calcul EMA 10, MA 35 (Lissée / RMA) et EMA 55."""
    df['ema_10'] = ta.trend.ema_indicator(df['close'], window=10)
    # RMA (Running Moving Average) identique à ta.rma(close, 35) dans TradingView
    df['ma_35'] = df['close'].ewm(alpha=1/35, adjust=False).mean()
    df['ema_55'] = ta.trend.ema_indicator(df['close'], window=55)
    return df

def generate_signal_and_calc_rr(df):
    """
    Stratégie calquée à 100 % sur Pine Script :
    - Cassure : EMA 10 sort au-dessus/en-dessous de MA 35 et EMA 55.
    - Retest : Unique 1er retest dans la fenêtre des 20 bougies (les retests suivants sont ignorés).
    - Filtre Tendance : close > MA 35 (pour BUY) ou close < MA 35 (pour SELL).
    - SL (10 bougies) / TP (60 bougies) + Filtre R:R >= 2.7.
    """
    if len(df) < 65:
        return None, "Données insuffisantes (< 65 bougies)", None, None, None

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = curr['close']
    
    signal_type = None
    setup_direction = None

    # Conditions de sortie globale de l'EMA 10 vis-à-vis de MA35 et EMA55
    df['ema_above_both'] = (df['ema_10'] > df['ma_35']) & (df['ema_10'] > df['ema_55'])
    df['ema_below_both'] = (df['ema_10'] < df['ma_35']) & (df['ema_10'] < df['ema_55'])

    # 1. Détection Cassure directe sur la dernière bougie
    breakout_up = df['ema_above_both'].iloc[-1] and not df['ema_above_both'].iloc[-2]
    breakout_down = df['ema_below_both'].iloc[-1] and not df['ema_below_both'].iloc[-2]

    if breakout_up:
        signal_type = "Cassure"
        setup_direction = "BUY"
    elif breakout_down:
        signal_type = "Cassure"
        setup_direction = "SELL"

    # 2. Détection du PREMIER Retest uniquement (Fenêtre max 20 bougies)
    if not setup_direction:
        # Recherche du dernier point de cassure dans l'historique récent (jusqu'à 20 bougies)
        lookback = df.iloc[-21:-1].copy() # Exclut la bougie actuelle pour chercher l'origine
        
        # Trouver la bougie où la cassure s'est produite
        breakout_up_indices = lookback.index[(lookback['ema_above_both']) & (~lookback['ema_above_both'].shift(1, fill_value=False))].tolist()
        breakout_down_indices = lookback.index[(lookback['ema_below_both']) & (~lookback['ema_below_both'].shift(1, fill_value=False))].tolist()

        if breakout_up_indices:
            last_breakout_idx = breakout_up_indices[-1]
            # Extraire les bougies entre la cassure et la bougie précédente
            sub_seq = df.loc[last_breakout_idx : df.index[-2]]
            
            # Vérifier si un retest A DÉJÀ eu lieu dans cette sous-séquence
            already_retested = any((sub_seq['low'] <= sub_seq['ema_10']) & (sub_seq['close'] > sub_seq['ema_10']))
            
            # Condition du 1er retest sur la bougie actuelle
            curr_retest = (curr['low'] <= curr['ema_10']) and (curr['close'] > curr['ema_10'])
            
            if curr_retest and not already_retested:
                signal_type = "Retest"
                setup_direction = "BUY"

        elif breakout_down_indices:
            last_breakout_idx = breakout_down_indices[-1]
            sub_seq = df.loc[last_breakout_idx : df.index[-2]]
            
            already_retested = any((sub_seq['high'] >= sub_seq['ema_10']) & (sub_seq['close'] < sub_seq['ema_10']))
            
            curr_retest = (curr['high'] >= curr['ema_10']) and (curr['close'] < curr['ema_10'])
            
            if curr_retest and not already_retested:
                signal_type = "Retest"
                setup_direction = "SELL"

    if not setup_direction:
        return None, "Pas de signal (Ni cassure ni 1er retest valide)", None, None, None

    # 3. Filtre de Tendance MA 35
    if setup_direction == "BUY" and curr['close'] <= curr['ma_35']:
        return None, "REJETÉ: Prix sous la MA 35 (Tendance Baissière)", None, None, None
    if setup_direction == "SELL" and curr['close'] >= curr['ma_35']:
        return None, "REJETÉ: Prix au-dessus de la MA 35 (Tendance Haussière)", None, None, None

    # 4. Calcul TP / SL (SL: 10 bougies, TP: 60 bougies)
    lookback_sl = df.iloc[-10:]
    lookback_tp = df.iloc[-60:]

    if setup_direction == "BUY":
        sl = lookback_sl['low'].min()
        tp = lookback_tp['high'].max()
        risk = curr_price - sl
        reward = tp - curr_price
    else: # SELL
        sl = lookback_sl['high'].max()
        tp = lookback_tp['low'].min()
        risk = sl - curr_price
        reward = curr_price - tp

    if risk <= 0 or reward <= 0:
        return None, f"Configuration invalide ({setup_direction}): SL/TP incohérent", sl, tp, 0

    rr_ratio = round(reward / risk, 2)

    # 5. Filtre Risk/Reward min 2.7
    if rr_ratio < 2.7:
        reason = f"Signal {setup_direction} ({signal_type}) REJETÉ: R:R = {rr_ratio} < 2.7 (SL: {sl:.4f}, TP: {tp:.4f})"
        return None, reason, sl, tp, rr_ratio

    valid_reason = f"Signal {setup_direction} ({signal_type}) VALIDE: R:R = {rr_ratio} >= 2.7"
    return setup_direction, valid_reason, sl, tp, rr_ratio

async def run_scan_job():
    global LAST_SCAN_LOGS
    logging.info("Lancement du scan des marchés...")
    
    LAST_SCAN_LOGS = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "details": [],
        "signals": []
    }
    signals_sent = 0
    
    for symbol, config in list(STATE.items()):
        if not config.get("enabled", True):
            LAST_SCAN_LOGS["details"].append(f"⚪ **{symbol}**: Désactivé")
            continue
        
        timeframe = config.get("timeframe", "4h")
        
        try:
            df = await fetch_ohlcv(symbol, timeframe=timeframe)
            await asyncio.sleep(2.0)
            
            if df is None or df.empty:
                LAST_SCAN_LOGS["details"].append(f"❌ **{symbol}**: Erreur de données OHLCV")
                continue
                
            df = calculate_indicators(df)
            signal, filter_reason, sl, tp, rr = generate_signal_and_calc_rr(df)
            
            curr_price = df.iloc[-1]['close']
            log_line = f"**{symbol}** ({timeframe}): Prix={curr_price} | Filter: {filter_reason}"
            LAST_SCAN_LOGS["details"].append(log_line)
            
            if signal:
                LAST_SCAN_LOGS["signals"].append(
                    f"🚨 **{signal}** sur **{symbol}** à {curr_price} (SL: {sl:.4f}, TP: {tp:.4f}, R:R: {rr})"
                )
                
                if telegram_app and TELEGRAM_CHAT_ID:
                    message = (
                        f"🚨 **SIGNAL DETECTED** 🚨\n\n"
                        f"**Asset:** {symbol}\n"
                        f"**Direction:** {signal}\n"
                        f"**Price:** {curr_price}\n"
                        f"**Stop-Loss:** {sl:.4f}\n"
                        f"**Take-Profit:** {tp:.4f}\n"
                        f"**Risk/Reward:** {rr}\n"
                        f"**Timeframe:** {timeframe}"
                    )
                    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
                    signals_sent += 1
        except Exception as e:
            logging.error(f"Erreur lors du traitement de {symbol}: {e}")
            LAST_SCAN_LOGS["details"].append(f"❌ **{symbol}**: Exception {e}")
            continue
                
    if telegram_app and TELEGRAM_CHAT_ID:
        try:
            await telegram_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, 
                text=f"✅ Scan terminé ({signals_sent} signal/s trouvé/s). Tapez /logs pour les détails."
            )
        except Exception as e:
            logging.error(f"Erreur envoi notification fin de scan : {e}")

async def scheduled_cron_loop():
    """Planificateur 4h fixe (00h, 04h, 08h, 12h, 16h, 20h UTC)."""
    while True:
        now = datetime.utcnow()
        next_hour = ((now.hour // 4) + 1) * 4
        if next_hour >= 24:
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            next_run = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
            
        wait_seconds = (next_run - now).total_seconds()
        logging.info(f"Prochain scan automatique à : {next_run.strftime('%Y-%m-%d %H:%M:%S UTC')} (attente: {int(wait_seconds)}s)")
        
        await asyncio.sleep(wait_seconds)
        await run_scan_job()

# Commandes Telegram
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot EMA Strategy (Premier Retest Uniquement) opérationnel ! Tapez /list pour vos actifs.")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LAST_SCAN_LOGS["timestamp"]:
        await update.message.reply_text("Aucun scan n'a encore été exécuté.")
        return

    msg = f"📊 **Rapport du Dernier Scan ({LAST_SCAN_LOGS['timestamp']})**\n\n"
    
    msg += "🚨 **Signaux Valides Détectés :**\n"
    if LAST_SCAN_LOGS["signals"]:
        for sig in LAST_SCAN_LOGS["signals"]:
            msg += f"{sig}\n"
    else:
        msg += "Aucun signal validé.\n"
        
    msg += "\n🔎 **Détail des Filtres Appliqués :**\n"
    for detail in LAST_SCAN_LOGS["details"]:
        msg += f"• {detail}\n"
        
    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STATE:
        await update.message.reply_text("Aucun actif enregistré.")
        return
    
    msg = "📋 **Liste des actifs suivis :**\n\n"
    for symbol, cfg in STATE.items():
        status = "✅" if cfg.get("enabled", True) else "❌"
        msg += f"{status} **{symbol}** | TF: {cfg.get('timeframe', '4h')}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /add_asset <SYMBOL> [timeframe]\nExemple: /add_asset SOL/USDT 4h")
        return
    
    symbol = args[0].upper()
    tf = args[1] if len(args) > 1 else "4h"
    
    STATE[symbol] = {"enabled": True, "timeframe": tf}
    success = save_state(STATE)
    
    if success:
        await update.message.reply_text(f"✅ Actif **{symbol}** ({tf}) ajouté et sauvegardé !", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ **{symbol}** ajouté localement, mais échec de sauvegarde sur Supabase.", parse_mode="Markdown")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /remove_asset <SYMBOL>\nExemple: /remove_asset BTC/USDT")
        return
    
    symbol = args[0].upper()
    if symbol in STATE:
        del STATE[symbol]
        success = save_state(STATE)
        if success:
            await update.message.reply_text(f"🗑️ Actif **{symbol}** supprimé et mis à jour sur Supabase !", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ **{symbol}** supprimé localement, mais échec de sauvegarde sur Supabase.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Actif {symbol} introuvable dans la liste.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Lancement du scan manuel...")
    asyncio.create_task(run_scan_job())

async def gainers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Récupération du Top Gainers 24h...")
    movers = await fetch_top_movers(limit=5, fetch_gainers=True)
    if not movers:
        await update.message.reply_text("⚠️ Impossible de récupérer les données Binance.")
        return
    
    msg = "🔥 **Top 5 Gainers Binance (24h) :**\n\n"
    for item in movers:
        msg += f"🟢 **{item['symbol']}** : +{item['change']:.2f}% | Prix: {item['price']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def losers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📉 Récupération du Top Losers 24h...")
    movers = await fetch_top_movers(limit=5, fetch_gainers=False)
    if not movers:
        await update.message.reply_text("⚠️ Impossible de récupérer les données Binance.")
        return
    
    msg = "🔻 **Top 5 Losers Binance (24h) :**\n\n"
    for item in movers:
        msg += f"🔴 **{item['symbol']}** : {item['change']:.2f}% | Prix: {item['price']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_scan_request(request):
    asyncio.create_task(run_scan_job())
    return web.Response(text="Scan Job triggered successfully.", status=200)

async def handle_ping_request(request):
    return web.Response(text="PONG", status=200)

async def main():
    global telegram_app
    
    server = web.Application()
    server.router.add_get('/scan', handle_scan_request)
    server.router.add_get('/ping', handle_ping_request)
    server.router.add_get('/', lambda r: web.Response(text="Signal Bot is Running.", status=200))
    
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        telegram_app.add_handler(CommandHandler("start", start_cmd))
        telegram_app.add_handler(CommandHandler("logs", logs_cmd))
        telegram_app.add_handler(CommandHandler("list", list_cmd))
        telegram_app.add_handler(CommandHandler("add_asset", add_asset_cmd))
        telegram_app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
        telegram_app.add_handler(CommandHandler("scan", scan_cmd))
        telegram_app.add_handler(CommandHandler("gainers", gainers_cmd))
        telegram_app.add_handler(CommandHandler("losers", losers_cmd))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        
        # Scan au lancement + Lancement du cron 4h fixe
        asyncio.create_task(run_scan_job())
        asyncio.create_task(scheduled_cron_loop())
        
        if TELEGRAM_CHAT_ID:
            try:
                await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, 
                    text="⚡ **Bot mis à jour ! Règle du premier retest unique activée (Leverage supprimé).**", 
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Erreur envoi notification de lancement : {e}")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
