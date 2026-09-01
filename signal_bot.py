import os
import asyncio
import logging
import json
import requests
import pandas as pd
import ccxt.async_support as ccxt
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from supabase import create_client, Client
from dotenv import load_dotenv
from aiohttp import web

# ==========================================
# CONFIGURATION & LOGGING
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_USERNAME = "@ideasanddreams"
ADMIN_ID = 1096334202
PORT = int(os.getenv("PORT", 8080))

# Initialisation de Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logging.error(f"Erreur initialisation Supabase : {e}")
    supabase = None

# Initialisation de l'exchange
exchange = ccxt.bybit({'enableRateLimit': True})

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
REJECTED_LOGS = deque(maxlen=50)
telegram_app: Application = None

# ==========================================
# UTILITAIRES ASYNCHRONES (DB & FORMATTAGE)
# ==========================================

async def supabase_execute(query):
    """Exécute une requête Supabase de manière non-bloquante."""
    if not supabase:
        return None
    try:
        res = await asyncio.to_thread(query.execute)
        return res
    except Exception as e:
        logging.error(f"Erreur DB: {e}")
        return None

def format_symbol(raw_symbol):
    """Gère le formatage crypto et respecte les indices Deriv."""
    s = raw_symbol.upper().strip()
    if any(x in s for x in ["VOL", "R_", "100", "75", "50", "25", "10", "BOOM", "CRASH"]):
        return s
    if "/" not in s:
        return f"{s}/USDT"
    return s

async def get_user_profile(telegram_id):
    res = await supabase_execute(supabase.table("users").select("*").eq("telegram_id", telegram_id))
    if res and res.data:
        return res.data[0]
    return None

# ==========================================
# INDICATEURS ET TRADING LOGIC
# ==========================================

async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
    if "VOL" in symbol or "R_" in symbol or "BOOM" in symbol or "CRASH" in symbol:
        raise ValueError("Actif Deriv détecté. API Bybit ignorée pour ce symbole.")
        
    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['EMA10'] = df['close'].ewm(span=10, adjust=False).mean()  
    df['SMA35'] = df['close'].rolling(window=35).mean()          
    df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()  
    return df

def get_last_pivot(df: pd.DataFrame, current_idx: int, window: int = 10, kind: str = "HIGH", condition: str = "NONE"):
    search_start = current_idx - window
    
    for p in range(search_start, window, -1):
        is_pivot = True
        for j in range(1, window + 1):
            if kind == "HIGH":
                if df['high'].iloc[p] <= df['high'].iloc[p - j] or df['high'].iloc[p] <= df['high'].iloc[p + j]:
                    is_pivot = False
                    break
            else: 
                if df['low'].iloc[p] >= df['low'].iloc[p - j] or df['low'].iloc[p] >= df['low'].iloc[p + j]:
                    is_pivot = False
                    break
        
        if is_pivot:
            if condition == "BELOW_EMAS":
                if (df['low'].iloc[p] < df['EMA10'].iloc[p] and df['low'].iloc[p] < df['SMA35'].iloc[p] and df['low'].iloc[p] < df['EMA55'].iloc[p]):
                    return df['low'].iloc[p]
            elif condition == "ABOVE_EMAS":
                if (df['high'].iloc[p] > df['EMA10'].iloc[p] and df['high'].iloc[p] > df['SMA35'].iloc[p] and df['high'].iloc[p] > df['EMA55'].iloc[p]):
                    return df['high'].iloc[p]
            else:
                return df['high'].iloc[p] if kind == "HIGH" else df['low'].iloc[p]
    
    lookback_start = max(0, current_idx - 50)
    return df['high'].iloc[lookback_start:current_idx].max() if kind == "HIGH" else df['low'].iloc[lookback_start:current_idx].min()

def analyze_market(df: pd.DataFrame, symbol: str, timeframe: str) -> dict:
    if len(df) < 60:
        return {"status": "NONE", "msg": "⚪ Historique insuffisant"}
    
    i = len(df) - 2
    last = df.iloc[i]
    prev = df.iloc[i-1]
    
    ema10, prev_ema10 = last['EMA10'], prev['EMA10']
    sma35, prev_sma35 = last['SMA35'], prev['SMA35']
    ema55, prev_ema55 = last['EMA55'], prev['EMA55']
    close = last['close']
    
    long_signal = (prev_ema10 <= prev_sma35) and (ema10 > sma35) and (ema10 > ema55)
    short_signal = (prev_ema10 >= prev_sma35) and (ema10 < sma35) and (ema10 < ema55)
    
    if long_signal:
        tp = get_last_pivot(df, i, window=10, kind="HIGH")
        sl = get_last_pivot(df, i, window=10, kind="LOW", condition="BELOW_EMAS")
        
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

    if ema10 > sma35 and ema10 > ema55:
        recent_cross = any((df['EMA10'].iloc[k-1] <= df['SMA35'].iloc[k-1] and df['EMA10'].iloc[k] > df['SMA35'].iloc[k]) for k in range(i, max(0, i - 15), -1))
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
# SERVEUR HEALTH CHECK
# ==========================================

async def handle_health(request):
    return web.Response(text="OK", status=200)

# ==========================================
# COMMANDES UTILISATEUR & GESTION D'ACCÈS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "SansPseudo"
    prof = await get_user_profile(uid)
    
    # 1. Utilisateur inconnu -> Notification Admin
    if not prof:
        await update.message.reply_text("⛔ <b>Accès restreint.</b>\nVotre demande a été envoyée à l'administrateur.", parse_mode="HTML")
        
        keyboard = [
            [InlineKeyboardButton("✅ Approuver (Free)", callback_data=f"allow_{uid}_free_{username}")],
            [InlineKeyboardButton("🌟 Approuver (Premium)", callback_data=f"allow_{uid}_premium_{username}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        admin_msg = f"🔔 <b>Nouvel Utilisateur en attente</b>\n\n👤 <b>ID:</b> <code>{uid}</code>\n🔖 <b>Pseudo:</b> @{username}\n\nApprouvez l'accès :"
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Erreur envoi notification admin : {e}")
        return

    # 2. Utilisateur validé -> Menu Complet
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
    """Gère le clic sur les boutons de notification Admin."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data.startswith("allow_"):
        parts = data.split("_")
        if len(parts) >= 3:
            target_id = int(parts[1])
            role = parts[2]
            target_username = parts[3] if len(parts) > 3 else "User"
            
            # Application des limites
            max_as = 5 if role == "free" else (20 if role == "premium" else 999)
            
            # Enregistrement base de données
            await supabase_execute(supabase.table("users").upsert({
                "telegram_id": target_id, 
                "username": target_username, 
                "role": role, 
                "max_assets": max_as
            }))
            
            # Mise à jour du message de l'admin
            await query.edit_message_text(f"✅ Utilisateur <code>{target_id}</code> (@{target_username}) approuvé avec succès en tant que <b>{role.upper()}</b>.", parse_mode="HTML")
            
            # Notification à l'utilisateur
            try:
                await context.bot.send_message(chat_id=target_id, text="🎉 <b>Votre accès a été approuvé !</b>\nTapez /start pour voir le menu complet.", parse_mode="HTML")
            except Exception as e:
                logging.error(f"Impossible de notifier l'utilisateur {target_id} : {e}")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await get_user_profile(update.effective_user.id): return
    if not REJECTED_LOGS:
        return await update.message.reply_text("📭 Aucun setup n'a été filtré récemment (R:R faible).")
        
    msg = "🗑️ <b>Derniers Setups Filtrés (R:R < 2.7) :</b>\n\n"
    for log in list(REJECTED_LOGS)[:20]:
        msg += f"• {log}\n"
    await update.message.reply_text(msg)

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

    res = await supabase_execute(supabase.table("assets").select("*").eq("telegram_id", uid))
    current_assets = res.data if res and res.data else []
    existing_symbols = [a['symbol'] for a in current_assets]

    added, errors = [], []
    for raw_symbol in args:
        symbol = format_symbol(raw_symbol)
        if len(current_assets) + len(added) >= prof['max_assets']:
            errors.append(f"⛔ Limite atteinte ({prof['max_assets']}) à partir de {symbol}.")
            break
        if symbol in existing_symbols or symbol in added:
            continue
        await supabase_execute(supabase.table("assets").insert({"telegram_id": uid, "symbol": symbol, "timeframe": tf}))
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
        res = await supabase_execute(supabase.table("assets").delete().eq("telegram_id", uid).eq("symbol", symbol))
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
        res = await supabase_execute(supabase.table("assets").update({"timeframe": tf}).eq("telegram_id", uid).eq("symbol", symbol))
        if res and res.data:
            updated.append(symbol)

    await update.message.reply_text(f"✅ TF <b>{tf}</b> sur : {', '.join(updated)}" if updated else "❌ Aucun trouvé.", parse_mode="HTML")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return

    res = await supabase_execute(supabase.table("assets").select("*").eq("telegram_id", uid))
    assets = res.data if res and res.data else []
    
    if not assets:
        return await update.message.reply_text("📭 Liste vide.")

    msg = "📊 <b>Actifs suivis :</b>\n\n" + "\n".join([f"🔸 <b>{a['symbol']}</b> (<code>{a['timeframe']}</code>)" for a in assets])
    await update.message.reply_text(msg, parse_mode="HTML")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await get_user_profile(uid): return

    msg = await update.message.reply_text("🔄 Analyse Bybit en cours (Bougies clôturées)...")
    res = await supabase_execute(supabase.table("assets").select("*").eq("telegram_id", uid))
    assets = res.data if res and res.data else []

    results = []
    for a in assets:
        try:
            df = await fetch_ohlcv_async(a['symbol'], a['timeframe'], 150)
            diag = analyze_market(df, a['symbol'], a['timeframe'])
            results.append(f"🔸 <b>{a['symbol']}</b> ({a['timeframe']})\n└ {diag['msg']}")
        except ValueError as ve:
            results.append(f"⚠️ <b>{a['symbol']}</b> : Ignoré (Actif Deriv non supporté via CCXT)")
        except Exception as e:
            results.append(f"⚠️ <b>{a['symbol']}</b> : Erreur Bybit ({e})")

    await msg.edit_text("\n\n".join(results) if results else "❌ Pas de données.", parse_mode="HTML")

# ==========================================
# COMMANDES ADMIN & BACKTEST
# ==========================================

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin" or len(context.args) < 2:
        return await update.message.reply_text("⚠️ Usage: /backtest <SYMBOLE> <JOURS> [tf]")

    symbol = format_symbol(context.args[0])
    jours = int(context.args[1])
    tf = context.args[2].lower() if len(context.args) > 2 else "1h"

    msg = await update.message.reply_text(f"⏳ Backtest <b>{symbol}</b> ({jours}j, {tf})...", parse_mode="HTML")

    try:
        tf_mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        limit = min(1000, int((jours * 1440) / tf_mins.get(tf, 60)) + 100)
        
        df = await fetch_ohlcv_async(symbol, tf, limit)
        trades, wins = 0, 0

        for i in range(100, len(df) - 2):
            ema10, p_ema10 = df['EMA10'].iloc[i], df['EMA10'].iloc[i-1]
            sma35, p_sma35 = df['SMA35'].iloc[i], df['SMA35'].iloc[i-1]
            ema55 = df['EMA55'].iloc[i]
            close = df['close'].iloc[i]

            long_sig = (p_ema10 <= p_sma35) and (ema10 > sma35) and (ema10 > ema55)
            short_sig = (p_ema10 >= p_sma35) and (ema10 < sma35) and (ema10 < ema55)

            if long_sig:
                tp = get_last_pivot(df, i, 10, "HIGH")
                sl = get_last_pivot(df, i, 10, "LOW", "BELOW_EMAS")
                if tp > close and sl < close and (tp - close) / (close - sl) >= 2.7:
                    trades += 1
                    for j in range(i+1, min(i+50, len(df))):
                        if df['high'].iloc[j] >= tp and df['low'].iloc[j] <= sl:
                            open_price = df['open'].iloc[j]
                            if abs(tp - open_price) < abs(open_price - sl):
                                wins += 1
                            break
                        elif df['high'].iloc[j] >= tp: 
                            wins += 1; break
                        elif df['low'].iloc[j] <= sl: 
                            break

            elif short_sig:
                tp = get_last_pivot(df, i, 10, "LOW")
                sl = get_last_pivot(df, i, 10, "HIGH", "ABOVE_EMAS")
                if tp < close and sl > close and (close - tp) / (sl - close) >= 2.7:
                    trades += 1
                    for j in range(i+1, min(i+50, len(df))):
                        if df['low'].iloc[j] <= tp and df['high'].iloc[j] >= sl:
                            open_price = df['open'].iloc[j]
                            if abs(open_price - tp) < abs(sl - open_price):
                                wins += 1
                            break
                        elif df['low'].iloc[j] <= tp: 
                            wins += 1; break
                        elif df['high'].iloc[j] >= sl: 
                            break

        winrate = (wins / trades * 100) if trades > 0 else 0
        await msg.edit_text(
            f"📈 <b>Backtest {symbol} ({tf}) :</b>\n\n🔹 Setups Valides (RR>2.7) : {trades}\n🔹 TP Atteints : {wins}\n🔹 Winrate Est. : <b>{winrate:.1f}%</b>", 
            parse_mode="HTML"
        )
    except ValueError as ve:
        await msg.edit_text(f"❌ Erreur : {ve}")
    except Exception as e:
        await msg.edit_text(f"❌ Erreur Bybit : {e}")

async def top_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    # Placeholder command
    await update.message.reply_text("🚧 La commande /top_scan est en cours de développement.")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    if not context.args: return
    
    target_id = int(context.args[0])
    role = context.args[1].lower() if len(context.args) > 1 else "free"
    max_as = 5 if role == "free" else (20 if role == "premium" else 999)
    await supabase_execute(supabase.table("users").upsert({"telegram_id": target_id, "username": "User", "role": role, "max_assets": max_as}))
    await update.message.reply_text(f"✅ Rôle {role} appliqué manuellement à {target_id}.")

async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    
    service_id = os.getenv('RENDER_SERVICE_ID')
    api_key = os.getenv('RENDER_API_KEY')
    
    if not service_id or not api_key:
        return await update.message.reply_text("❌ Variables Render non configurées (RENDER_SERVICE_ID, RENDER_API_KEY).")
    
    try:
        requests.post(f"https://api.render.com/v1/services/{service_id}/restart", 
                     headers={"Authorization": f"Bearer {api_key}"})
        await update.message.reply_text("🔄 Redémarrage du serveur Render lancé avec succès.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur redémarrage : {e}")


# ==========================================
# SCAN AUTOMATIQUE D'ARRIÈRE-PLAN
# ==========================================

async def run_scan_job():
    if not supabase: return
    
    while True:
        try:
            res = await supabase_execute(supabase.table("assets").select("*"))
            assets = res.data if res and res.data else []
            if not assets:
                await asyncio.sleep(900)
                continue

            for asset in assets:
                try:
                    df = await fetch_ohlcv_async(asset['symbol'], asset['timeframe'], 150)
                    diag = analyze_market(df, asset['symbol'], asset['timeframe'])
                    
                    if diag["status"] == "SIGNAL":
                        await telegram_app.bot.send_message(
                            chat_id=asset['telegram_id'],
                            text=f"🔔 <b>ALERTE {asset['symbol']}</b> ({asset['timeframe']})\n\n{diag['msg']}",
                            parse_mode="HTML"
                        )
                except ValueError:
                    pass 
                except Exception as e:
                    logging.error(f"Erreur scan {asset['symbol']}: {e}")
            
            await asyncio.sleep(900)
        except Exception as e:
            logging.error(f"Erreur run_scan_job : {e}")
            await asyncio.sleep(300)

# ==========================================
# DÉMARRAGE & MAIN
# ==========================================

async def post_init(application: Application):
    try:
        await application.bot.send_message(chat_id=ADMIN_ID, text="✅ <b>Bot Signal V0.5 En Ligne.</b>", parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Impossible d'envoyer message de démarrage : {e}")

async def main():
    global telegram_app
    
    server = web.Application()
    server.router.add_get('/', handle_health)
    server.router.add_get('/health', handle_health)

    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN manquant.")
        return

    telegram_app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
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

    asyncio.create_task(run_scan_job())
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
