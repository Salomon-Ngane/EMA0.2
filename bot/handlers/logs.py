from telegram import Update
from telegram.ext import ContextTypes
from database.users import get_user_profile
from bot.handlers.scan import REJECTED_LOGS

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return
    
    if not REJECTED_LOGS:
        return await update.message.reply_text("📭 Aucun setup n'a été filtré récemment (R:R faible).")
        
    msg = "🗑️ <b>Derniers Setups Filtrés (R:R < 2.7) :</b>\n\n"
    for log in list(REJECTED_LOGS)[:20]:
        msg += f"• {log}\n"
    await update.message.reply_text(msg)
