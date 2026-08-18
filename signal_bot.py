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

# Default Assets Configuration
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
    # Validation du timeframe pour éviter l'erreur 'Invalid interval'
    valid_timeframes = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
    if not timeframe or timeframe not in valid_timeframes:
        timeframe = "4h"

    exchange = ccxt.binance({
        'enableRateLimit': True, 
        'rateLimit': 2000, # Délai augmenté pour éviter le bannissement IP
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
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema_50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    return df

def generate_signal(df):
    if len(df) < 2:
        return None, "Données insuffisantes"
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if prev['ema_20'] <= prev['ema_50'] and curr['ema_20'] > curr['ema_50']:
        return "BUY", f"Croisement Haussier (EMA20={curr['ema_20']:.2f} > EMA50={curr['ema_50']:.2f})"
    elif prev['ema_20'] >= prev['ema_50'] and curr['ema_20'] < curr['ema_50']:
        return "SELL", f"Croisement Baissier (EMA20={curr['ema_20']:.2f} < EMA50={curr['ema_50']:.2f})"
    
    return None, f"Pas de croisement (EMA20={curr['ema_20']:.2f}, EMA50={curr['ema_50']:.2f})"

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
            await asyncio.sleep(2.0) # Pause de 2s entre chaque actif pour respecter l'IP
            
            if df is None or df.empty:
                LAST_SCAN_LOGS["details"].append(f"❌ **{symbol}**: Erreur de données OHLCV")
                continue
                
            df = calculate_indicators(df)
            signal, filter_reason = generate_signal(df)
            
            curr_price = df.iloc[-1]['close']
            rsi_val = round(df.iloc[-1]['rsi'], 2)
            
            log_line = f"**{symbol}** ({timeframe}): Prix={curr_price} | RSI={rsi_val} | Filter: {filter_reason}"
            LAST_SCAN_LOGS["details"].append(log_line)
            
            if signal:
                LAST_SCAN_LOGS["signals"].append(f"🚨 **{signal}** sur **{symbol}** à {curr_price} (RSI: {rsi_val})")
                
                if telegram_app and TELEGRAM_CHAT_ID:
                    message = (
                        f"🚨 **SIGNAL DETECTED** 🚨\n\n"
                        f"**Asset:** {symbol}\n"
                        f"**Direction:** {signal}\n"
                        f"**Price:** {curr_price}\n"
                        f"**RSI:** {rsi_val}\n"
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
                text=f"✅ Scan terminé ({signals_sent} signal/s trouvé/s). Utilisez /logs pour voir l'analyse détaillée."
            )
        except Exception as e:
            logging.error(f"Erreur envoi notification fin de scan : {e}")

# Commandes Telegram
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot EMA Signal opérationnel ! Utilisez /list pour voir les actifs configurés.")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LAST_SCAN_LOGS["timestamp"]:
        await update.message.reply_text("Aucun scan n'a encore été exécuté depuis le dernier démarrage.")
        return

    msg = f"📊 **Rapport du Dernier Scan ({LAST_SCAN_LOGS['timestamp']})**\n\n"
    
    msg += "🚨 **Signaux Détectés :**\n"
    if LAST_SCAN_LOGS["signals"]:
        for sig in LAST_SCAN_LOGS["signals"]:
            msg += f"{sig}\n"
    else:
        msg += "Aucun signal détecté.\n"
        
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
                    text="⚡ **Bot mis à jour ! Scan automatique lancé.**\nCommandes : /logs, /scan, /list", 
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Erreur envoi notification de lancement : {e}")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
