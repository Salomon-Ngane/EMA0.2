import os
import asyncio
import requests
import pandas as pd
import ccxt
from collections import deque
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

# Stockage en mémoire des signaux rejetés (RR < 2.7)
REJECTED_LOGS = deque(maxlen=50)

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

def fetch_ohlcv_sync(symbol: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
    """Récupère les bougies et calcule les indicateurs : EMA10, SMA35, EMA55."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()  # Verte
    df['SMA35'] = df['close'].rolling(window=35).mean()          # Rouge
    df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()  # Jaune
    return df

def get_last_pivot(df: pd.DataFrame, start_idx: int, window: int = 10, kind: str = "HIGH", condition: str = "NONE"):
    """Recherche algorithmique du dernier sommet/creux confirmé sur 'window' bougies."""
    for i in range(start_idx - 1, window, -1):
        is_pivot = True
        for j in range(1, window + 1):
            if kind == "HIGH":
                if df['high'].iloc[i] <= df['high'].iloc[i - j] or df['high'].iloc[i] <= df['high'].iloc[i + j]:
                    is_pivot = False
                    break
            else: # LOW
                if df['low'].iloc[i] >= df['low'].iloc[i - j] or df['low'].iloc[i] >= df['low'].iloc[i + j]:
                    is_pivot = False
                    break
        
        if is_pivot:
            if condition == "BELOW_EMAS":
                if (df['low'].iloc[i] < df['EMA10'].iloc[i] and 
                    df['low'].iloc[i] < df['SMA35'].iloc[i] and 
                    df['low'].iloc[i] < df['EMA55'].iloc[i]):
                    return df['low'].iloc[i]
            elif condition == "ABOVE_EMAS":
                if (df['high'].iloc[i] > df['EMA10'].iloc[i] and 
                    df['high'].iloc[i] > df['SMA35'].iloc[i] and 
                    df['high'].iloc[i] > df['EMA55'].iloc[i]):
                    return df['high'].iloc[i]
            else:
                return df['high'].iloc[i] if kind == "HIGH" else df['low'].iloc[i]
    
    # Sécurité si aucun pivot net n'est trouvé dans l'historique proche
    lookback_start = max(0, start_idx - 50)
    return df['high'].iloc[lookback_start:start_idx].max() if kind == "HIGH" else df['low'].iloc[lookback_start:start_idx].min()

def analyze_market(df: pd.DataFrame, symbol: str, timeframe: str) -> dict:
    """Génère le setup complet en vérifiant les cassures, le R:R et les retests."""
    if len(df) < 60:
        return {"status": "NONE", "msg": "⚪ Historique insuffisant"}
    
    i = len(df) - 1
    last = df.iloc[i]
    prev = df.iloc[i-1]
    
    ema10, prev_ema10 = last['EMA10'], prev['EMA10']
    sma35, prev_sma35 = last['SMA35'], prev['SMA35']
    ema55, prev_ema55 = last['EMA55'], prev['EMA55']
    close = last['close']
    
    # --- LOGIQUE DE CASSURE PRINCIPALE ---
    # LONG : L'EMA 10 passe au-dessus de la Rouge(SMA35) en étant déjà au-dessus de la Jaune(EMA55)
    long_signal = (prev_ema10 <= prev_sma35) and (ema10 > sma35) and (ema10 > ema55)
    # SHORT : L'EMA 10 passe en-dessous de la Rouge(SMA35) en étant déjà en-dessous de la Jaune(EMA55)
    short_signal = (prev_ema10 >= prev_sma35) and (ema10 < sma35) and (ema10 < ema55)
    
    if long_signal:
        tp = get_last_pivot(df, i, window=10, kind="HIGH")
        sl = get_last_pivot(df, i, window=10, kind="LOW", condition="BELOW_EMAS")
        
        # Validations logiques basiques
        if tp > close and sl < close:
            rr = abs(tp - close) / abs(close - sl)
            if rr >= 2.7:
                return {"status": "SIGNAL", "msg": f"🚀 <b>SIGNAL ACHAT</b> (Long)\n🔹 Entrée: {close}\n🎯 TP: {tp:.4f}\n🛑 SL: {sl:.4f}\n⚖️ R:R: {rr:.2f}"}
            else:
                REJECTED_LOGS.appendleft(f"[{symbol} {timeframe}] ACHAT rejeté | R:R = {rr:.2f} (TP={tp:.2f}, SL={sl:.2f})")
                return {"status": "NONE", "msg": f"🟢 Tendance Haussière (Signal écarté R:R={rr:.2f})"}
    
    elif short_signal:
        tp = get_last_pivot(df, i, window=10, kind="LOW")
        sl = get_last_pivot(df, i, window=10, kind="HIGH", condition="ABOVE_EMAS")
        
        if tp < close and sl > close:
            rr = abs(close - tp) / abs(sl - close)
            if rr >= 2.7:
                return {"status": "SIGNAL", "msg": f"⚠️ <b>SIGNAL VENTE</b> (Short)\n🔹 Entrée: {close}\n🎯 TP: {tp:.4f}\n🛑 SL: {sl:.4f}\n⚖️ R:R: {rr:.2f}"}
            else:
                REJECTED_LOGS.appendleft(f"[{symbol} {timeframe}] VENTE rejetée | R:R = {rr:.2f} (TP={tp:.2f}, SL={sl:.2f})")
                return {"status": "NONE", "msg": f"🔴 Tendance Baissière (Signal écarté R:R={rr:.2f})"}

    # --- LOGIQUE DE RETEST ---
    if ema10 > sma35 and ema10 > ema55:
        # Vérifie si une cassure a eu lieu récemment (15 dernières bougies)
        recent_cross = any((df['EMA10'].iloc[k-1] <= df['SMA35'].iloc[k-1] and df['EMA10'].iloc[k] > df['SMA35'].iloc[k]) for k in range(i, max(0, i - 15), -1))
        # Condition du Retest Long : Le Low touche EMA10 pour la première fois, sans casser la structure
        if recent_cross and last['low'] <= ema10 and prev['low'] > prev['EMA10'] and close > sma35 and close > ema55:
            return {"status": "SIGNAL", "msg": f"🔄 <b>RETEST ACHAT</b> (Confirmation)\n🔹 Prix: {close}\n└ Rebord sur l'EMA 10 maintenu."}
        return {"status": "NONE", "msg": "🟢 Tendance Haussière"}
        
    elif ema10 < sma35 and ema10 < ema55:
        recent_cross = any((df['EMA10'].iloc[k-1] >= df['SMA35'].iloc[k-1] and df['EMA10'].iloc[k] < df['SMA35'].iloc[k]) for k in range(i, max(0, i - 15), -1))
        if recent_cross and last['high'] >= ema10 and prev['high'] < prev['EMA10'] and close < sma35 and close < ema55:
            return {"status": "SIGNAL", "msg": f"🔄 <b>RETEST VENTE</b> (Confirmation)\n🔹 Prix: {close}\n└ Plafond sur l'EMA 10 maintenu."}
        return {"status": "NONE", "msg": "🔴 Tendance Baissière"}
        
    return {"status": "NONE", "msg": "⚪ Neutre (Structure non alignée)"}

# ==========================================
# COMMANDES UTILISATEUR
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text(f"⛔ <b>Accès restreint.</b>\nContactez l'admin : {ADMIN_USERNAME}", parse_mode="HTML")
        return

    prof = get_user_profile(uid)
    await update.message.reply_text(
        f"👋 <b>Bienvenue dans le Bot Signal v0.4 (Pro Strategy)</b>\n\n"
        f"👤 <b>Statut :</b> {prof['role'].upper()}\n\n"
        "📋 <b>Commandes Utilisateur :</b>\n"
        "/list — Voir vos actifs\n"
        "/add_asset <code>&lt;sym1&gt; [sym2...] [tf]</code>\n"
        "/remove_asset <code>&lt;sym1&gt; [sym2...]</code>\n"
        "/set_tf <code>&lt;tf&gt; &lt;sym1&gt; [sym2...]</code>\n"
        "/scan — Lancer une analyse manuelle\n"
        "/logs — Voir les setups ignorés (R:R < 2.7)\n\n"
        "🛠 <b>Commandes Admin :</b>\n"
        "/backtest <code>&lt;symbole&gt; &lt;jours&gt; [tf]</code>\n"
        "/allow <code>&lt;id&gt; [role]</code> | /restart",
        parse_mode="HTML"
    )

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return
        
    if not REJECTED_LOGS:
        await update.message.reply_text("📭 Aucun setup n'a été filtré récemment (R:R faible).")
        return
        
    msg = "🗑️ <b>Derniers Setups Filtrés (R:R < 2.7) :</b>\n\n"
    for log in list(REJECTED_LOGS)[:20]: # Affiche les 20 plus récents
        msg += f"• {log}\n"
        
    await update.message.reply_text(msg)

async def add_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add_asset &lt;symbole1&gt; [symbole2...] [timeframe]</code>", parse_mode="HTML")
        return

    args = list(context.args)
    tf = "1h"
    if args[-1].lower() in VALID_TIMEFRAMES:
        tf = args.pop(-1).lower()

    prof = get_user_profile(uid)
    current_assets = supabase.table("assets").select("*").eq("telegram_id", uid).execute().data
    existing_symbols = [a['symbol'] for a in current_assets]

    added, errors = [], []
    for raw_symbol in args:
        symbol = raw_symbol.upper() if "/" in raw_symbol.upper() else f"{raw_symbol.upper()}/USDT"

        if len(current_assets) + len(added) >= prof['max_assets']:
            errors.append(f"⛔ Limite atteinte ({prof['max_assets']}) à partir de {symbol}.")
            break

        if symbol in existing_symbols or symbol in added:
            continue

        supabase.table("assets").insert({"telegram_id": uid, "symbol": symbol, "timeframe": tf}).execute()
        added.append(symbol)

    msg = (f"✅ <b>Ajoutés (TF: {tf}) :</b> {', '.join(added)}\n" if added else "") + "\n".join(errors)
    await update.message.reply_text(msg if msg else "❌ Aucun actif ajouté.", parse_mode="HTML")

async def remove_asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: <code>/remove_asset &lt;symbole&gt;</code>", parse_mode="HTML")

    removed = []
    for raw_symbol in context.args:
        symbol = raw_symbol.upper() if "/" in raw_symbol.upper() else f"{raw_symbol.upper()}/USDT"
        if supabase.table("assets").delete().eq("telegram_id", uid).eq("symbol", symbol).execute().data:
            removed.append(symbol)

    await update.message.reply_text(f"🗑️ <b>Retirés :</b> {', '.join(removed)}" if removed else "❌ Introuvable.", parse_mode="HTML")

async def set_tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid) or len(context.args) < 2:
        return
        
    tf = context.args[0].lower()
    if tf not in VALID_TIMEFRAMES:
        return await update.message.reply_text(f"❌ Timeframe invalide.")

    updated = []
    for raw_symbol in context.args[1:]:
        symbol = raw_symbol.upper() if "/" in raw_symbol.upper() else f"{raw_symbol.upper()}/USDT"
        if supabase.table("assets").update({"timeframe": tf}).eq("telegram_id", uid).eq("symbol", symbol).execute().data:
            updated.append(symbol)

    await update.message.reply_text(f"✅ TF <b>{tf}</b> sur : {', '.join(updated)}" if updated else "❌ Aucun trouvé.", parse_mode="HTML")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    assets = supabase.table("assets").select("*").eq("telegram_id", uid).execute().data
    if not assets:
        return await update.message.reply_text("📭 Liste vide.")

    msg = "📊 <b>Actifs :</b>\n\n" + "\n".join([f"🔸 <b>{a['symbol']}</b> (<code>{a['timeframe']}</code>)" for a in assets])
    await update.message.reply_text(msg, parse_mode="HTML")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        return

    msg = await update.message.reply_text("🔄 Analyse en cours (Stratégie Pro)...")
    assets = supabase.table("assets").select("*").eq("telegram_id", uid).execute().data

    results = []
    for a in assets:
        try:
            df = await asyncio.to_thread(fetch_ohlcv_sync, a['symbol'], a['timeframe'], 150)
            diag = analyze_market(df, a['symbol'], a['timeframe'])
            results.append(f"🔸 <b>{a['symbol']}</b> ({a['timeframe']})\n└ {diag['msg']}")
        except Exception as e:
            results.append(f"⚠️ <b>{a['symbol']}</b> : Erreur ({e})")

    await msg.edit_text("\n\n".join(results), parse_mode="HTML")

# ==========================================
# COMMANDES ADMIN & BACKTEST
# ==========================================

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    if not prof or prof.get("role") != "admin" or len(context.args) < 2:
        return await update.message.reply_text("⚠️ /backtest <SYMBOLE> <JOURS> [tf]")

    symbol = context.args[0].upper()
    if "/" not in symbol: symbol += "/USDT"
    jours = int(context.args[1])
    tf = context.args[2].lower() if len(context.args) > 2 else "1h"

    msg = await update.message.reply_text(f"⏳ Backtest <b>{symbol}</b> ({jours}j, {tf})...", parse_mode="HTML")

    try:
        tf_mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        limit = min(1000, int((jours * 1440) / tf_mins.get(tf, 60)) + 100) # +100 pour historique initial
        
        df = await asyncio.to_thread(fetch_ohlcv_sync, symbol, tf, limit)
        
        trades, wins = 0, 0

        # Simulation bougie par bougie
        for i in range(100, len(df) - 1):
            ema10, p_ema10 = df['EMA10'].iloc[i], df['EMA10'].iloc[i-1]
            sma35, p_sma35 = df['SMA35'].iloc[i], df['SMA35'].iloc[i-1]
            ema55 = df['EMA55'].iloc[i]
            close = df['close'].iloc[i]

            long_sig = (p_ema10 <= p_sma35) and (ema10 > sma35) and (ema10 > ema55)
            short_sig = (p_ema10 >= p_sma35) and (ema10 < sma35) and (ema10 < ema55)

            if long_sig:
                tp = get_last_pivot(df, i, 10, "HIGH")
                sl = get_last_pivot(df, i, 10, "LOW", "BELOW_EMAS")
                if tp > close and sl < close:
                    rr = (tp - close) / (close - sl)
                    if rr >= 2.7:
                        trades += 1
                        for j in range(i+1, min(i+50, len(df))): # Simulation forward 50 bougies
                            if df['high'].iloc[j] >= tp: wins += 1; break
                            elif df['low'].iloc[j] <= sl: break

            elif short_sig:
                tp = get_last_pivot(df, i, 10, "LOW")
                sl = get_last_pivot(df, i, 10, "HIGH", "ABOVE_EMAS")
                if tp < close and sl > close:
                    rr = (close - tp) / (sl - close)
                    if rr >= 2.7:
                        trades += 1
                        for j in range(i+1, min(i+50, len(df))):
                            if df['low'].iloc[j] <= tp: wins += 1; break
                            elif df['high'].iloc[j] >= sl: break

        winrate = (wins / trades * 100) if trades > 0 else 0
        await msg.edit_text(
            f"📈 <b>Backtest {symbol} ({tf}) :</b>\n\n🔹 Setups Valides (RR>2.7) : {trades}\n🔹 TP Atteints : {wins}\n🔹 Winrate Est. : <b>{winrate:.1f}%</b>", 
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Erreur : {e}")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Même logique qu'avant
    uid = update.effective_user.id
    prof = get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    if len(context.args) < 1: return
    target_id, role = int(context.args[0]), context.args[1].lower() if len(context.args) > 1 else "free"
    max_as = 5 if role == "free" else (20 if role == "premium" else 999)
    supabase.table("users").upsert({"telegram_id": target_id, "username": "User", "role": role, "max_assets": max_as}).execute()
    await update.message.reply_text(f"✅ Rôle {role} appliqué à {target_id}.")

async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Même logique qu'avant
    if get_user_profile(update.effective_user.id).get("role") != "admin": return
    requests.post(f"https://api.render.com/v1/services/{os.getenv('RENDER_SERVICE_ID')}/restart", headers={"Authorization": f"Bearer {os.getenv('RENDER_API_KEY')}"})
    await update.message.reply_text("🔄 Redémarrage Render lancé.")

# ==========================================
# SCAN AUTOMATIQUE D'ARRIÈRE-PLAN
# ==========================================

async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    assets = supabase.table("assets").select("*").execute().data
    if not assets: return

    for asset in assets:
        try:
            df = await asyncio.to_thread(fetch_ohlcv_sync, asset['symbol'], asset['timeframe'], 150)
            diag = analyze_market(df, asset['symbol'], asset['timeframe'])
            
            if diag["status"] == "SIGNAL":
                await context.bot.send_message(
                    chat_id=asset['telegram_id'],
                    text=f"🔔 <b>ALERTE {asset['symbol']}</b> ({asset['timeframe']})\n\n{diag['msg']}",
                    parse_mode="HTML"
                )
        except Exception as e:
            print(f"Erreur background {asset['symbol']}: {e}")

# ==========================================
# DÉMARRAGE ET MAIN
# ==========================================

async def post_init(application: ApplicationBuilder):
    try: await application.bot.send_message(chat_id=ADMIN_ID, text="✅ <b>Bot Signal V0.4 (Pro) En Ligne.</b>", parse_mode="HTML")
    except: pass

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add_asset", add_asset_cmd))
    app.add_handler(CommandHandler("remove_asset", remove_asset_cmd))
    app.add_handler(CommandHandler("set_tf", set_tf_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))

    app.job_queue.run_repeating(run_scan, interval=900, first=30)
    app.run_polling()

if __name__ == "__main__":
    main()
