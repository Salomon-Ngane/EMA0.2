import os
import asyncio
import logging
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
    "BTC/USDT": {"enabled": True, "timeframe": "1h", "leverage": 10},
    "ETH/USDT": {"enabled": True, "timeframe": "1h", "leverage": 10},
    "SOL/USDT": {"enabled": True, "timeframe": "1h", "leverage": 10},
    "BNB/USDT": {"enabled": True, "timeframe": "1h", "leverage": 10},
    "XRP/USDT": {"enabled": True, "timeframe": "1h", "leverage": 10},
}

def load_state():
    """Charge la liste des actifs depuis Supabase ou renvoie la liste par défaut."""
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
        return
        
    try:
        supabase.table("state").update({"data": state}).eq("id", 1).execute()
    except Exception as e:
        logging.error(f"Erreur sauvegarde Supabase : {e}")

# Charge l'état global au lancement
STATE = load_state()
telegram_app = None

async def fetch_ohlcv(symbol, timeframe="1h", limit=100):
    exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
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

def calculate_indicators(df):
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema_50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    return df

def generate_signal(df):
    if len(df) < 2:
        return None
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if prev['ema_20'] <= prev['ema_50'] and curr['ema_20'] > curr['ema_50']:
        return "BUY"
    elif prev['ema_20'] >= prev['ema_50'] and curr['ema_20'] < curr['ema_50']:
        return "SELL"
    return None

async def run_scan_job():
    logging.info("Lancement du scan des marchés...")
    signals_sent = 0
    
    for symbol, config in list(STATE.items()):
        if not config.get("enabled", True):
            continue
        
        timeframe = config.get("timeframe", "1h")
        leverage = config.get("leverage", 10)
        
        df = await fetch_ohlcv(symbol, timeframe=timeframe)
        if df is None or df.empty:
            continue
            
        df = calculate_indicators(df)
        signal = generate_signal(df)
        
        if signal and telegram_app and TELEGRAM_CHAT_ID:
            curr_price = df.iloc[-1]['close']
            rsi_val = round(df.iloc[-1]['rsi'], 2)
            
            message = (
                f"🚨 **SIGNAL DETECTED** 🚨\n\n"
                f"**Asset:** {symbol}\n"
                f"**Direction:** {signal}\n"
                f"**Price:** {curr_price}\n"
                f"**RSI:** {rsi_val}\n"
                f"**Timeframe:** {timeframe}\n"
                f"**Leverage:** {leverage}x"
            )
            try:
                await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
                signals_sent += 1
            except Exception as e:
                logging.error(f"Erreur envoi message Telegram : {e}")
                
    # Message de confirmation de fin de scan
    if telegram_app and TELEGRAM_CHAT_ID:
        try:
            await telegram_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, 
                text=f"✅ Scan terminé ({signals_sent} signal/s trouvé/s). Le bot reste opérationnel."
            )
        except Exception as e:
            logging.error(f"Erreur envoi notification fin de scan : {e}")

# Commandes Telegram
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot EMA Signal opérationnel ! Utilisez /list pour voir les actifs configurés.")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STATE:
        await update.message.reply_text("Aucun actif enregistré.")
        return
    
    msg = "📋 **Liste des actifs suivis :**\n\n"
    for symbol, cfg in STATE.items():
        status = "✅" if cfg.get("enabled", True) else "❌"
        msg += f"{status} **{symbol}** | TF: {cfg.get('timeframe', '1h')} | Lev: {cfg.get('leverage', 10)}x\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /add_asset <SYMBOL> [timeframe] [leverage]\nExemple: /add_asset SOL/USDT 1h 10")
        return
    
    symbol = args[0].upper()
    tf = args[1] if len(args) > 1 else "1h"
    lev = int(args[2]) if len(args) > 2 else 10
    
    STATE[symbol] = {"enabled": True, "timeframe": tf, "leverage": lev}
    save_state(STATE)
    
    await update.message.reply_text(f"✅ Actif **{symbol}** ajouté et enregistré !", parse_mode="Markdown")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /remove_asset <SYMBOL>")
        return
    
    symbol = args[0].upper()
    if symbol in STATE:
        del STATE[symbol]
        save_state(STATE)
        await update.message.reply_text(f"🗑️ Actif **{symbol}** supprimé !", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Actif {symbol} introuvable.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Lancement du scan manuel...")
    asyncio.create_task(run_scan_job())

async def handle_scan_request(request):
    asyncio.create_task(run_scan_job())
    return web.Response(text="Scan Job triggered successfully.")

async def main():
    global telegram_app
    
    # 1. Démarrage du serveur Web (pour Cron-Job)
    server = web.Application()
    server.router.add_get('/scan', handle_scan_request)
    server.router.add_get('/', lambda r: web.Response(text="Signal Bot is Running."))
    
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    # 2. Initialisation du Bot Telegram
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        telegram_app.add_handler(CommandHandler("start", start_cmd))
        telegram_app.add_handler(CommandHandler("list", list_cmd))
        telegram_app.add_handler(CommandHandler("add_asset", add_asset_cmd))
        telegram_app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
        telegram_app.add_handler(CommandHandler("scan", scan_cmd))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
