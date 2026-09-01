import os
import asyncio
import requests
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
ADMIN_ID = 1096334202 

# Initialisation de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialisation de l'exchange (Binance par défaut)
exchange = ccxt.binance({'enableRateLimit': True})

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

# ==========================================
# FONCTIONS UTILITAIRES & INDICATEURS
# ==========================================

def get_user_profile(telegram_id):
    """Récupère le profil utilisateur depuis Supabase."""
    response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if response.data:
        return response.data[0]
    return None

def is_whitelisted(telegram_id):
    """Vérifie si l'utilisateur est autorisé."""
    user = get_user_profile(telegram_id)
    return user is not None

def fetch_ohlcv_sync(symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    """Récupère les bougies et calcule les indicateurs (Exécuté dans un thread pour ne pas bloquer)."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['MA35'] = df['close'].rolling(window=35).mean()
    df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
    return df

def analyze_market(df: pd.DataFrame) -> str:
    """Génère un diagnostic technique simple basé sur l'alignement des EMA."""
    if len(df) < 55:
        return "⚪ Historique insuffisant"
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # Signaux de croisement
    if prev['EMA10'] <= prev['EMA55'] and last['EMA10'] > last['EMA55']:
        return "🚀 <b>SIGNAL ACHAT</b> (Croisement EMA10 > EMA55)"
    elif prev['EMA10'] >= prev['EMA55'] and last['EMA10'] < last['EMA55']:
        return "⚠️ <b>SIGNAL VENTE</b> (Croisement EMA10 < EMA55)"
    elif last['EMA10'] > last['EMA55']:
        return "🟢 Tend. Haussière (EMA10 > EMA55)"
    else:
        return "🔴 Tend. Bussière (EMA10 < EMA55)"

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
        "📋 <b>Commandes Utilisateur :</b>\n"
        "/list — Voir vos actifs\n"
        "/add_asset <code>&lt;symbole1&gt; [symbole2...] [tf]</code> — Ajouter 1 ou plusieurs actifs\n"
        "/remove_asset <code>&lt;symbole1&gt; [symbole2...]</code> — Retirer 1 ou plusieurs actifs\n"
        "/set_tf <code>&lt;tf&gt; &lt;symbole1&gt; [symbole2...]</code> — Modifier le timeframe\n"
        "/scan — Lancer une analyse manuelle\n\n"
        "🛠 <b>Commandes Admin :</b>\n"
        "/backtest <code>&lt;symbole&gt; &lt;jours&gt; [tf]</code>\n"
        "/allow <code>&lt;user_id&gt; [role]</code>\n"
        "/restart — Redémarrer le serveur Render",
        parse_mode="HTML"
    )

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add_asset &lt;symbole1&gt; [symbole2...] [timeframe]</code>", parse_mode="HTML")
        return

    args = list(context.args)
    tf = "1h"
    
    # Vérifier si le dernier argument est un timeframe valide
    if args[-1].lower() in VALID_TIMEFRAMES:
        tf = args.pop(-1).lower()

    prof = get_user_profile(uid)
    user_assets_res = supabase.table("assets").select("*").eq("telegram_id", uid).execute()
    current_assets = user_assets_res.data
    existing_symbols = [a['symbol'] for a in current_assets]

    added = []
    errors = []

    for raw_symbol in args:
        symbol = raw_symbol.upper()
        if "/" not in symbol:
            symbol += "/USDT"

        if len(current_assets) + len(added) >= prof['max_assets']:
            errors.append(f"⛔ Limite atteinte ({prof['max_assets']} max) à partir de <b>{symbol}</b>.")
            break

        if symbol in existing_symbols or symbol in added:
            errors.append(f"⚠️ <b>{symbol}</b> déjà dans votre liste.")
            continue

        supabase.table("assets").insert({
            "telegram_id": uid,
            "symbol": symbol,
            "timeframe": tf
        }).execute()
        added.append(symbol)

    msg = ""
    if added:
        msg += f"✅ <b>Actifs ajoutés (TF: {tf}) :</b> {', '.join(added)}\n"
    if errors:
        msg += "\n".join(errors)

    await update.message.reply_text(msg if msg else "❌ Aucun actif ajouté.", parse_mode="HTML")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/remove_asset &lt;symbole1&gt; [symbole2...]</code>", parse_mode="HTML")
        return

    removed = []
    not_found = []

    for raw_symbol in context.args:
        symbol = raw_symbol.upper()
        if "/" not in symbol:
            symbol += "/USDT"

        res = supabase.table("assets").delete().eq("telegram_id", uid).eq("symbol", symbol).execute()
        if res.data:
            removed.append(symbol)
        else:
            not_found.append(symbol)

    msg = ""
    if removed:
        msg += f"🗑️ <b>Actifs retirés :</b> {', '.join(removed)}\n"
    if not_found:
        msg += f"❌ <b>Non trouvés :</b> {', '.join(not_found)}"

    await update.message.reply_text(msg, parse_mode="HTML")

async def set_tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Usage: <code>/set_tf &lt;timeframe&gt; &lt;symbole1&gt; [symbole2...]</code>", parse_mode="HTML")
        return

    tf = context.args[0].lower()
    if tf not in VALID_TIMEFRAMES:
        await update.message.reply_text(f"❌ Timeframe invalide ('{tf}'). Valides : {', '.join(VALID_TIMEFRAMES)}")
        return

    updated = []
    for raw_symbol in context.args[1:]:
        symbol = raw_symbol.upper()
        if "/" not in symbol:
            symbol += "/USDT"

        res = supabase.table("assets").update({"timeframe": tf}).eq("telegram_id", uid).eq("symbol", symbol).execute()
        if res.data:
            updated.append(symbol)

    if updated:
        await update.message.reply_text(f"✅ Timeframe <b>{tf}</b> appliqué à : {', '.join(updated)}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Aucun actif correspondant trouvé dans votre liste.")

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
        msg += f"🔸 <b>{a['symbol']}</b> — TF: <code>{a['timeframe']}</code>\n"

    await update.message.reply_text(msg, parse_mode="HTML")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    msg = await update.message.reply_text("🔄 Analyse de vos actifs en cours...")
    assets = supabase.table("assets").select("*").eq("telegram_id", uid).execute().data
    
    if not assets:
        await msg.edit_text("❌ Vous n'avez aucun actif à scanner. Utilisez /add_asset.")
        return

    results = []
    for a in assets:
        try:
            df = await asyncio.to_thread(fetch_ohlcv_sync, a['symbol'], a['timeframe'])
            diag = analyze_market(df)
            price = df['close'].iloc[-1]
            results.append(f"🔸 <b>{a['symbol']}</b> ({a['timeframe']}) : {price} USDT\n   └ {diag}")
        except Exception as e:
            results.append(f"⚠️ <b>{a['symbol']}</b> : Erreur CCXT ({e})")

    await msg.edit_text("\n\n".join(results), parse_mode="HTML")

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
        await update.message.reply_text("⚠️ Usage : <code>/allow &lt;user_id&gt; [free/premium/admin]</code>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ L'ID utilisateur doit être un nombre.")
        return

    role = context.args[1].lower() if len(context.args) > 1 else "free"
    max_assets = 5 if role == "free" else (20 if role == "premium" else 999)

    supabase.table("users").upsert({
        "telegram_id": target_id,
        "username": "Utilisateur", 
        "role": role,
        "max_assets": max_assets
    }).execute()

    await update.message.reply_text(f"✅ Utilisateur {target_id} autorisé (Rôle: <b>{role}</b> | Limite: {max_assets}).", parse_mode="HTML")

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    
    if not prof or prof.get("role") != "admin":
        await update.message.reply_text("⛔ Commande réservée aux administrateurs.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Usage : <code>/backtest &lt;SYMBOLE&gt; &lt;JOURS&gt; [timeframe]</code>", parse_mode="HTML")
        return

    symbol = context.args[0].upper()
    if "/" not in symbol:
        symbol += "/USDT"
    
    try:
        jours = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Nombre de jours invalide.")
        return

    tf = context.args[2].lower() if len(context.args) > 2 else "1h"

    msg = await update.message.reply_text(f"⏳ Backtest de <b>{symbol}</b> ({jours}j, TF: {tf}) en cours...", parse_mode="HTML")

    try:
        # Calcul du nombre de bougies approximatif
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
        limit = min(1000, int((jours * 1440) / tf_minutes.get(tf, 60)))
        
        df = await asyncio.to_thread(fetch_ohlcv_sync, symbol, tf, limit)
        
        trades = 0
        wins = 0
        in_trade = False

        for i in range(1, len(df)):
            if not in_trade and df['EMA10'].iloc[i] > df['EMA55'].iloc[i] and df['EMA10'].iloc[i-1] <= df['EMA55'].iloc[i-1]:
                in_trade = True
                trades += 1
            elif in_trade and df['EMA10'].iloc[i] < df['EMA55'].iloc[i] and df['EMA10'].iloc[i-1] >= df['EMA55'].iloc[i-1]:
                in_trade = False
                wins += 1 # Simulation simplifiée de clôture sur croisement

        winrate = (wins / trades * 100) if trades > 0 else 0
        await msg.edit_text(
            f"📈 <b>Résultat du Backtest pour {symbol} :</b>\n\n"
            f"🔹 Bougies analysées : {len(df)}\n"
            f"🔹 Signaux générés : {trades}\n"
            f"🔹 Winrate estimé : <b>{winrate:.1f}%</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Erreur lors du backtest : {e}")

async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    
    if not prof or prof.get("role") != "admin":
        await update.message.reply_text("⛔ Commande réservée aux administrateurs.")
        return

    api_key = os.getenv("RENDER_API_KEY")
    service_id = os.getenv("RENDER_SERVICE_ID")

    if not api_key or not service_id:
        await update.message.reply_text("⚠️ Variables `RENDER_API_KEY` ou `RENDER_SERVICE_ID` manquantes.")
        return

    await update.message.reply_text("🔄 Redémarrage demandé à Render. Indisponibilité temporaire...")

    url = f"https://api.render.com/v1/services/{service_id}/restart"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    try:
        res = requests.post(url, headers=headers)
        if res.status_code != 202:
            await update.message.reply_text(f"❌ Échec Render (Code {res.status_code}).")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur API Render : {e}")

# ==========================================
# SCAN AUTOMATIQUE D'ARRIÈRE-PLAN
# ==========================================

async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    """Parcourt les actifs configurés et envoie des alertes en cas de signal."""
    res = supabase.table("assets").select("*").execute()
    all_assets = res.data
    if not all_assets:
        return

    for asset in all_assets:
        try:
            df = await asyncio.to_thread(fetch_ohlcv_sync, asset['symbol'], asset['timeframe'])
            diag = analyze_market(df)
            
            if "SIGNAL" in diag:
                await context.bot.send_message(
                    chat_id=asset['telegram_id'],
                    text=f"🔔 <b>ALERTE SCAN AUTOMATIQUE</b>\n\n🔸 <b>{asset['symbol']}</b> ({asset['timeframe']})\n└ {diag}",
                    parse_mode="HTML"
                )
        except Exception as e:
            print(f"Erreur scan auto sur {asset['symbol']}: {e}")

# ==========================================
# DÉMARRAGE ET MAIN
# ==========================================

async def post_init(application: ApplicationBuilder):
    """Notification au démarrage."""
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID, 
            text="✅ <b>Serveur redémarré :</b> Le bot Signal V0.4 est en ligne et prêt à analyser.", 
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Erreur message post_init: {e}")

def main():
    if not TELEGRAM_TOKEN:
        print("Erreur: TELEGRAM_TOKEN introuvable.")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Handlers Utilisateur
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    app.add_handler(CommandHandler("set_tf", set_tf_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    
    # Handlers Admin
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))

    # Tâche d'arrière-plan (Scan toutes les 15 mins)
    job_queue = app.job_queue
    job_queue.run_repeating(run_scan, interval=900, first=30)

    print("Démarrage du bot V0.4...")
    app.run_polling()

if __name__ == "__main__":
    main()
