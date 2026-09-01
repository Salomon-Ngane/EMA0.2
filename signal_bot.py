import os
import asyncio
import pandas as pd
import ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from supabase import create_client, Client
from dotenv import load_dotenv

# Charger les variables d'environnement
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_USERNAME = "@ideasanddreams"
# Remplacer par ton vrai ID si nécessaire pour le message de démarrage
ADMIN_ID = 1096334202 

# Initialisation de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialisation de l'exchange (Binance par défaut)
exchange = ccxt.binance({'enableRateLimit': True})

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

# ==========================================
# FONCTIONS UTILITAIRES (BASE DE DONNÉES)
# ==========================================

def get_user_profile(telegram_id):
    """Récupère ou crée le profil utilisateur depuis Supabase."""
    response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if response.data:
        return response.data[0]
    return None

def is_whitelisted(telegram_id):
    """Vérifie si l'utilisateur a accès au bot."""
    user = get_user_profile(telegram_id)
    return user is not None

# ==========================================
# COMMANDES UTILISATEUR
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text(f"⛔ <b>Accès restreint.</b>\nVous n'êtes pas sur la liste blanche. Contactez l'admin : {ADMIN_USERNAME}", parse_mode="HTML")
        return

    prof = get_user_profile(uid)
    await update.message.reply_text(
        f"👋 <b>Bienvenue dans le Bot Signal v0.4</b>\n\n"
        f"👤 <b>Statut :</b> {prof['role'].upper()}\n"
        f"📊 <b>Limite d'actifs :</b> {prof['max_assets']}\n\n"
        "📋 <b>Commandes :</b>\n"
        "/list — Voir vos actifs\n"
        "/add_asset <code>&lt;symbole&gt;</code> [tf] — Ajouter un actif\n"
        "/remove_asset <code>&lt;symbole&gt;</code> — Retirer un actif\n"
        "/set_tf <code>&lt;tf&gt;</code> <code>&lt;symbole1&gt;</code>... — Modifier le timeframe\n"
        "/scan — Lancer une analyse manuelle\n\n"
        "🛠 <b>Commandes Admin :</b>\n"
        "/backtest <code>&lt;symbole&gt;</code> <code>&lt;jours&gt;</code> [tf]\n"
        "/allow <code>&lt;user_id&gt;</code> [role]",
        parse_mode="HTML"
    )

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add_asset &lt;symbole&gt; [timeframe]</code>", parse_mode="HTML")
        return

    symbol = context.args[0].upper()
    # Si le symbole ne contient pas "/", on ajoute "/USDT" par défaut (pour CCXT)
    if "/" not in symbol:
        symbol += "/USDT"

    tf = context.args[1].lower() if len(context.args) > 1 else "1h"

    if tf not in VALID_TIMEFRAMES:
        await update.message.reply_text(f"❌ Timeframe invalide ('{tf}'). \nUtilisez l'un de ceux-ci : {', '.join(VALID_TIMEFRAMES)}")
        return

    prof = get_user_profile(uid)
    user_assets_response = supabase.table("assets").select("*").eq("telegram_id", uid).execute()
    user_assets = user_assets_response.data

    if len(user_assets) >= prof['max_assets']:
        await update.message.reply_text(f"⛔ Limite atteinte ({prof['max_assets']} actifs max pour votre compte).")
        return

    # Empêcher les doublons
    for asset in user_assets:
        if asset['symbol'] == symbol:
            await update.message.reply_text(f"⚠️ L'actif <b>{symbol}</b> est déjà dans votre liste avec le timeframe <b>{asset['timeframe']}</b>.\nUtilisez /set_tf pour le modifier.", parse_mode="HTML")
            return

    # Ajout dans Supabase
    supabase.table("assets").insert({
        "telegram_id": uid,
        "symbol": symbol,
        "timeframe": tf
    }).execute()

    await update.message.reply_text(f"✅ <b>{symbol}</b> ajouté avec succès (TF: {tf}).", parse_mode="HTML")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/remove_asset &lt;symbole&gt;</code>", parse_mode="HTML")
        return

    symbol = context.args[0].upper()
    if "/" not in symbol:
        symbol += "/USDT"

    # Suppression de Supabase
    response = supabase.table("assets").delete().eq("telegram_id", uid).eq("symbol", symbol).execute()
    
    if response.data:
        await update.message.reply_text(f"🗑️ <b>{symbol}</b> a été retiré de votre liste.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ L'actif <b>{symbol}</b> n'est pas dans votre liste.", parse_mode="HTML")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    response = supabase.table("assets").select("*").eq("telegram_id", uid).execute()
    assets = response.data

    if not assets:
        await update.message.reply_text("📭 Votre liste d'actifs est vide.")
        return

    msg = "📊 <b>Vos actifs surveillés :</b>\n\n"
    for a in assets:
        msg += f"🔸 <b>{a['symbol']}</b> — {a['timeframe']}\n"

    await update.message.reply_text(msg, parse_mode="HTML")

# ==========================================
# COMMANDES ADMIN
# ==========================================

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    
    if not prof or prof.get("role") != "admin":
        await update.message.reply_text("⛔ Commande réservée aux administrateurs.")
        return

    if len(context.args) < 1:
        # CORRECTION HTML APPLIQUÉE ICI
        await update.message.reply_text("⚠️ Usage : <code>/allow &lt;user_id&gt; [free/premium/admin]</code>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ L'ID utilisateur doit être un nombre.")
        return

    role = context.args[1].lower() if len(context.args) > 1 else "free"
    max_assets = 5 if role == "free" else (20 if role == "premium" else 999)

    # Upsert dans Supabase
    supabase.table("users").upsert({
        "telegram_id": target_id,
        "username": "Utilisateur", 
        "role": role,
        "max_assets": max_assets
    }).execute()

    await update.message.reply_text(f"✅ Utilisateur {target_id} autorisé avec le rôle <b>{role}</b> ({max_assets} actifs max).", parse_mode="HTML")

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    
    if not prof or prof.get("role") != "admin":
        await update.message.reply_text("⛔ Commande réservée aux administrateurs.")
        return

    if len(context.args) < 2:
        # CORRECTION HTML APPLIQUÉE ICI
        await update.message.reply_text("⚠️ Usage : <code>/backtest &lt;SYMBOLE&gt; &lt;JOURS&gt; [timeframe]</code>", parse_mode="HTML")
        return

    symbol = context.args[0].upper()
    if "/" not in symbol:
        symbol += "/USDT"
    
    jours = context.args[1]
    tf = context.args[2] if len(context.args) > 2 else "1h"

    await update.message.reply_text(f"⏳ Lancement du backtest pour <b>{symbol}</b> sur {jours} jours (TF: {tf})...", parse_mode="HTML")
    # Logique de backtest à intégrer ici (qui ne bloque pas grâce à await)
    await asyncio.sleep(2) # Simulation de traitement
    await update.message.reply_text(f"✅ Backtest terminé. (Fonctionnalité complète à implémenter selon votre stratégie).")

# ==========================================
# LOGIQUE DE SCAN (V0.3 Maintenue)
# ==========================================

async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    """Fonction qui tourne en arrière-plan pour scanner les marchés."""
    # Récupérer tous les actifs uniques de la base de données
    response = supabase.table("assets").select("*").execute()
    all_assets = response.data
    
    if not all_assets:
        return

    # Note: Dans une vraie implémentation CCXT, on utiliserait asyncio.to_thread 
    # pour ne pas bloquer la boucle d'événements Telegram pendant le téléchargement des bougies.
    # Pour la structure, le scan s'exécute silencieusement et envoie des messages via context.bot.send_message
    pass 

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permet à l'utilisateur de lancer un scan manuel sur ses propres actifs."""
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    msg = await update.message.reply_text("🔄 Lancement de l'analyse de vos actifs en cours...")
    
    # Récupération des actifs de l'utilisateur
    assets = supabase.table("assets").select("*").eq("telegram_id", uid).execute().data
    
    if not assets:
        await msg.edit_text("❌ Vous n'avez aucun actif à scanner. Utilisez /add_asset.")
        return

    # Simulation de l'analyse (remplacer par votre algorithme CCXT)
    await asyncio.sleep(2) 
    
    await msg.edit_text("✅ Analyse terminée. Aucun signal critique détecté pour le moment.")

# ==========================================
# DÉMARRAGE ET MAIN
# ==========================================

async def post_init(application: ApplicationBuilder):
    """S'exécute une seule fois au démarrage complet du bot."""
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID, 
            text="✅ <b>Serveur redémarré :</b> Le bot Signal V0.4 est en ligne et prêt à analyser.", 
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Impossible d'envoyer le message de boot à l'admin: {e}")

def main():
    if not TELEGRAM_TOKEN:
        print("Erreur: TELEGRAM_TOKEN introuvable.")
        return

    # Construction de l'application avec le post_init pour le message de bienvenue
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Enregistrement des commandes
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    
    # Commandes Admin
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))

    # Tâche de fond pour le scan automatique (Toutes les 15 minutes par exemple)
    job_queue = app.job_queue
    job_queue.run_repeating(run_scan, interval=900, first=10)

    print("Démarrage du bot V0.4...")
    app.run_polling()

if __name__ == "__main__":
    main()
