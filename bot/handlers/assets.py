from telegram import Update
from telegram.ext import ContextTypes
from config import VALID_TIMEFRAMES
from database.users import get_user_profile
from database.assets import get_user_assets, add_asset, remove_asset, update_asset_tf
from services.router import is_deriv_symbol

def format_symbol(raw_symbol):
    s = raw_symbol.upper().strip()
    if is_deriv_symbol(s):
        return s
    if "/" not in s:
        return f"{s}/USDT"
    return s

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return

    assets = await get_user_assets(uid)
    if not assets:
        return await update.message.reply_text("📭 Liste vide.")

    msg = "📊 <b>Actifs suivis :</b>\n\n" + "\n".join([f"🔸 <b>{a['symbol']}</b> (<code>{a['timeframe']}</code>)" for a in assets])
    await update.message.reply_text(msg, parse_mode="HTML")

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof: return

    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/add_asset &lt;symbole1&gt; [symbole2...] [timeframe]</code>", parse_mode="HTML")

    args = list(context.args)
    tf = "1h"
    if args[-1].lower() in VALID_TIMEFRAMES:
        tf = args.pop(-1).lower()

    current_assets = await get_user_assets(uid)
    existing_symbols = [a['symbol'] for a in current_assets]

    added, errors = [], []
    for raw_symbol in args:
        symbol = format_symbol(raw_symbol)
        if len(current_assets) + len(added) >= prof['max_assets']:
            errors.append(f"⛔ Limite atteinte ({prof['max_assets']}) à partir de {symbol}.")
            break
        if symbol in existing_symbols or symbol in added:
            continue
        await add_asset(uid, symbol, tf)
        added.append(symbol)

    msg = (f"✅ <b>Ajoutés (TF: {tf}) :</b> {', '.join(added)}\n" if added else "") + "\n".join(errors)
    await update.message.reply_text(msg if msg.strip() else "❌ Aucun actif ajouté.", parse_mode="HTML")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/remove_asset &lt;symbole1&gt; [symbole2...]</code>", parse_mode="HTML")

    removed = []
    for raw_symbol in context.args:
        symbol = format_symbol(raw_symbol)
        res = await remove_asset(uid, symbol)
        if res and res.data:
            removed.append(symbol)

    await update.message.reply_text(f"🗑️ <b>Retirés :</b> {', '.join(removed)}" if removed else "❌ Introuvable.", parse_mode="HTML")

async def set_tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid) or len(context.args) < 2: return
        
    tf = context.args[0].lower()
    if tf not in VALID_TIMEFRAMES:
        return await update.message.reply_text("❌ Timeframe invalide.")

    updated = []
    for raw_symbol in context.args[1:]:
        symbol = format_symbol(raw_symbol)
        res = await update_asset_tf(uid, symbol, tf)
        if res and res.data:
            updated.append(symbol)

    await update.message.reply_text(f"✅ TF <b>{tf}</b> sur : {', '.join(updated)}" if updated else "❌ Aucun trouvé.", parse_mode="HTML")
