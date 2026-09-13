from telegram import Update
from telegram.ext import ContextTypes
from config import ADMIN_ID
from database.users import get_user_profile, upsert_user
from bot.keyboards import get_approval_keyboard

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "SansPseudo"
    prof = await get_user_profile(uid)
    
    if not prof:
        await update.message.reply_text("⛔ <b>Accès restreint.</b>\nVotre demande a été envoyée à l'administrateur.", parse_mode="HTML")
        admin_msg = f"🔔 <b>Nouvel Utilisateur en attente</b>\n\n👤 <b>ID:</b> <code>{uid}</code>\n🔖 <b>Pseudo:</b> @{username}\n\nApprouvez l'accès :"
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=get_approval_keyboard(uid, username), parse_mode="HTML")
        except Exception as e:
            print(f"Erreur envoi notification admin: {e}")
        return

    role = prof.get('role', 'free').upper()
    max_assets = prof.get('max_assets', 5)

    msg_text = (
        f"👋 <b>Bienvenue dans le Bot Signal</b>\n\n"
        f"👤 <b>Statut :</b> {role}\n"
        f"📊 <b>Limite d'actifs :</b> {max_assets}\n\n"
        "📋 <b>Commandes :</b>\n"
        "/list — Voir vos actifs\n"
        "/add_asset <code>&lt;symbole&gt; [tf]</code> — Ajouter un actif\n"
        "/remove_asset <code>&lt;symbole&gt;</code> — Retirer un actif\n"
        "/set_tf <code>&lt;tf&gt; &lt;symbole1&gt; &lt;symbole2&gt;...</code> — Modifier le timeframe\n"
        "/scan — Lancer une analyse manuelle\n"
        "/logs — Voir les setups ignorés (R:R < 2.7)\n\n"
        "🛠 <b>Commandes Admin :</b>\n"
        "/backtest <code>&lt;symbole&gt; &lt;jours&gt; [tf]</code>\n"
        "/top_scan <code>[jours]</code>\n"
        "/allow <code>&lt;user_id&gt; [role]</code> — Approuver/Modifier un rôle\n"
        "/restart — Redémarrer le service sur render"
    )
    await update.message.reply_text(msg_text, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("allow_"):
        parts = data.split("_")
        if len(parts) >= 3:
            target_id = int(parts[1])
            role = parts[2]
            target_username = parts[3] if len(parts) > 3 else "User"
            max_as = 5 if role == "free" else (20 if role == "premium" else 999)
            
            await upsert_user(target_id, target_username, role, max_as)
            await query.edit_message_text(f"✅ Utilisateur <code>{target_id}</code> (@{target_username}) approuvé en tant que <b>{role.upper()}</b>.", parse_mode="HTML")
            
            try:
                await context.bot.send_message(chat_id=target_id, text="🎉 <b>Votre accès a été approuvé !</b>\nTapez /start pour voir le menu.", parse_mode="HTML")
            except Exception as e:
                print(f"Erreur notification user: {e}")
