from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from database.users import get_user_profile
from database.assets import get_user_assets
from services.router import fetch_ohlcv_routed
from services.strategy import analyze_market

REJECTED_LOGS = deque(maxlen=50)

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return

    msg = await update.message.reply_text("🔄 Analyse en cours...")
    assets = await get_user_assets(uid)

    results = []
    for a in assets:
        try:
            df = await fetch_ohlcv_routed(a['symbol'], a['timeframe'], 150)
            diag = analyze_market(df, a['symbol'], a['timeframe'], REJECTED_LOGS)
            results.append(f"🔸 <b>{a['symbol']}</b> ({a['timeframe']})\n└ {diag['msg']}")
        except Exception as e:
            results.append(f"⚠️ <b>{a['symbol']}</b> : Erreur ({e})")

    await msg.edit_text("\n\n".join(results) if results else "❌ Pas de données.", parse_mode="HTML")
