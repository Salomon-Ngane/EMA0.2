import os
import asyncio
import logging
from datetime import datetime
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

# Default Assets Configuration (Timeframe par défaut : 4h)
DEFAULT_ASSETS = {
    "BTC/USDT": {"enabled": True, "timeframe": "4h", "leverage": 10},
    "ETH/USDT": {"enabled": True, "timeframe": "4h", "leverage": 10},
    "SOL/USDT": {"enabled": True, "timeframe": "4h", "leverage": 10},
    "BNB/USDT": {"enabled": True, "timeframe": "4h", "leverage": 10},
    "XRP/USDT": {"enabled": True, "timeframe": "4h", "leverage": 10},
}

# Mémoire globale pour stocker les logs du dernier scan
LAST_SCAN_LOGS = {
    "timestamp": None,
    "details": [],
    "signals": []
}

def load_state():
    """Charge la liste des actifs depuis Supabase ou renvoie la liste par défaut en 4h."""
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
    """Sauvegarde la liste des actifs dans Supabase."""
    if not supabase:
        logging.error("Impossible de sauvegarder : Supabase non configuré.")
        return False
        
    try:
        supabase.table("state").update({"data": state}).eq("id", 1).execute()
        return True
    except Exception as e:
        logging.error(f"Erreur sauvegarde Supabase : {e}")
        return False

# Charge l'état global au lancement
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
    """Récupère les plus fortes hausses ou baisses sur 24h sur Binance (Spot USDT)."""
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
    """Calcul des nouvelles moyennes mobiles EMA 10, EMA 35 et EMA 55."""
    df['ema_10'] = ta.trend.ema_indicator(df['close'], window=10)
    df['ema_35'] = ta.trend.ema_indicator(df['close'], window=35)
    df['ema_55'] = ta.trend.ema_indicator(df['close'], window=55)
    return df

def generate_signal_and_calc_rr(df):
    """
    Détecte les signaux de Cassure ou Retest (fenêtre de 5 bougies)
    et applique le filtre Risk/Reward >= 2.7 basé sur les Pivots (SL: 10, TP: 60).
    """
    if len(df) < 65:
        return None, "Données insuffisantes (< 65 bougies)", None, None, None

    curr = df.iloc[-1]
    curr_price = curr['close']
    
    signal_type = None
    setup_direction = None

    # 1. Détection de Cassure directe sur la dernière bougie
    prev = df.iloc[-2]
    if prev['ema_10'] <= prev['ema_55'] and curr['ema_10'] > curr['ema_55']:
        signal_type = "Cassure"
        setup_direction = "BUY"
    elif prev['ema_10'] >= prev['ema_55'] and curr['ema_10'] < curr['ema_55']:
        signal_type = "Cassure"
        setup_direction = "SELL"

    # 2. Détection de Retest dans la fenêtre des 5 dernières bougies si pas de cassure directe
    if not setup_direction:
        window = df.iloc[-6:-1] # 5 bougies précédentes
        
        # Condition Retest BUY : EMA 10 > EMA 55 globale, mais pullback du prix/EMA10 touche EMA 35 ou EMA 55
        has_bull_cross = any(window['ema_10'] > window['ema_55'])
        retest_bull = any((window['low'] <= window['ema_35']) | (window['low'] <= window['ema_55']))
        if has_bull_cross and retest_bull and curr['ema_10'] > curr['ema_35'] and curr['close'] > curr['ema_10']:
            signal_type = "Retest"
            setup_direction = "BUY"

        # Condition Retest SELL : EMA 10 < EMA 55 globale, mais pullback du prix/EMA10 touche EMA 35 ou EMA 55
        has_bear_cross = any(window['ema_10'] < window['ema_55'])
        retest_bear = any((window['high'] >= window['ema_35']) | (window['high'] >= window['ema_55']))
        if has_bear_cross and retest_bear and curr['ema_10'] < curr['ema_35'] and curr['close'] < curr['ema_10']:
            signal_type = "Retest"
            setup_direction = "SELL"

    if not setup_direction:
        return None, "Pas de signal (Pas de cassure ni retest valide)", None, None, None

    # 3. Calcul dynamique du SL (Pivots 10 bougies) et TP (Pivots 60 bougies)
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
        return None, f"Configuration invalide ({setup_direction}): Risk ou Reward négatif", sl, tp, 0

    rr_ratio = round(reward / risk, 2)

    # 4. Filtre strict Risk/Reward >= 2.7
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
        leverage = config.get("leverage", 10)
        
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
                        f"**Timeframe:** {timeframe}\n"
                        f"**Leverage:** {leverage}x"
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
                text=f"✅ Scan terminé ({signals_sent} signal/s trouvé/s). Utilisez /logs pour le détail."
            )
        except Exception as e:
            logging.error(f"Erreur envoi notification fin de scan : {e}")

# Commandes Telegram
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot EMA Strategy (10/35/55 + R:R 2.7) opérationnel ! Utilisez /list pour voir vos actifs.")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LAST_SCAN_LOGS["timestamp"]:
        await update.message.reply_text("Aucun scan n'a encore été exécuté depuis le dernier démarrage.")
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
        msg += f"{status} **{symbol}** | TF: {cfg.get('timeframe', '4h')} | Lev: {cfg.get('leverage', 10)}x\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /add_asset <SYMBOL> [timeframe] [leverage]\nExemple: /add_asset SOL/USDT 4h 10")
        return
    
    symbol = args[0].upper()
    tf = args[1] if len(args) > 1 else "4h"
    lev = int(args[2]) if len(args) > 2 else 10
    
    STATE[symbol] = {"enabled": True, "timeframe": tf, "leverage": lev}
    success = save_state(STATE)
    
    if success:
        await update.message.reply_text(f"✅ Actif **{symbol}** ({tf}, {lev}x) ajouté et sauvegardé !", parse_mode="Markdown")
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
        
        logging.info("Démarrage du scan initial...")
        asyncio.create_task(run_scan_job())
        
        if TELEGRAM_CHAT_ID:
            try:
                await telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, 
                    text="⚡ **Bot mis à jour avec la stratégie EMA 10/35/55 + R:R >= 2.7 !**\nCommandes : /logs, /scan, /list, /gainers, /losers", 
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Erreur envoi notification de lancement : {e}")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

