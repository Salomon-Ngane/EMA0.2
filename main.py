import asyncio
import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from config import TELEGRAM_TOKEN, PORT, ADMIN_ID
from services.health_server import start_health_server
from services.router import fetch_ohlcv_routed
from services.strategy import analyze_market
from database.assets import get_all_assets
from bot.handlers.start import start_cmd, button_handler
from bot.handlers.assets import list_cmd, add_asset_cmd, remove_asset_cmd, set_tf_cmd
from bot.handlers.scan import scan_cmd, REJECTED_LOGS
from bot.handlers.admin import allow_cmd, top_scan_cmd, restart_cmd, backtest_cmd
from bot.handlers.logs import logs_cmd

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

telegram_app: Application = None

async def run_full_scan():
    """Scan unique exécuté au démarrage (déclenché par le ping HTTP de cron-job.org)."""
    logging.info("Lancement du scan complet au démarrage...")
    assets = await get_all_assets()
    if not assets:
        logging.info("Aucun actif enregistré pour le scan.")
        return

    for asset in assets:
        try:
            df = await fetch_ohlcv_routed(asset['symbol'], asset['timeframe'], 150)
            diag = analyze_market(df, asset['symbol'], asset['timeframe'], REJECTED_LOGS)
            
            if diag["status"] == "SIGNAL":
                if telegram_app and telegram_app.bot:
                    await telegram_app.bot.send_message(
                        chat_id=asset['telegram_id'],
                        text=f"🔔 <b>ALERTE {asset['symbol']}</b> ({asset['timeframe']})\n\n{diag['msg']}",
                        parse_mode="HTML"
                    )
        except Exception as e:
            logging.error(f"Erreur scan {asset['symbol']}: {e}")

async def post_init(application: Application):
    try:
        await application.bot.send_message(chat_id=ADMIN_ID, text="✅ <b>Bot Signal V0.6 Modulaire En Ligne.</b>", parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Impossible d'envoyer le message de démarrage à l'admin: {e}")

async def main():
    global telegram_app
    
    # Démarrage du serveur HTTP pour Render & cron-job.org
    await start_health_server(PORT)
    logging.info(f"Serveur HTTP /health démarré sur le port {PORT}")

    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN manquant.")
        return

    telegram_app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Enregistrement des handlers
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("allow", allow_cmd))
    telegram_app.add_handler(CommandHandler("list", list_cmd))
    telegram_app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    telegram_app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    telegram_app.add_handler(CommandHandler("set_tf", set_tf_cmd))
    telegram_app.add_handler(CommandHandler("backtest", backtest_cmd))
    telegram_app.add_handler(CommandHandler("scan", scan_cmd))
    telegram_app.add_handler(CommandHandler("logs", logs_cmd))
    telegram_app.add_handler(CommandHandler("top_scan", top_scan_cmd))
    telegram_app.add_handler(CommandHandler("restart", restart_cmd))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logging.info("Bot Telegram v0.6 démarré.")

    # Lancement du scan unique au démarrage
    asyncio.create_task(run_full_scan())

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
