import requests
from telegram import Update
from telegram.ext import ContextTypes
from database.users import get_user_profile, upsert_user
from services.router import fetch_ohlcv_routed
from services.strategy import evaluate_setup
from config import RENDER_SERVICE_ID, RENDER_API_KEY
from bot.handlers.assets import format_symbol

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    if not context.args: return
    
    target_id = int(context.args[0])
    role = context.args[1].lower() if len(context.args) > 1 else "free"
    max_as = 5 if role == "free" else (20 if role == "premium" else 999)
    await upsert_user(target_id, "User", role, max_as)
    await update.message.reply_text(f"✅ Rôle {role} appliqué manuellement à {target_id}.")

async def top_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    await update.message.reply_text("🚧 La commande /top_scan est en cours de développement.")

async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_user_profile(uid)
    if not prof or prof.get("role") != "admin": return
    
    if not RENDER_SERVICE_ID or not RENDER_API_KEY:
        return await update.message.reply_text("❌ Variables Render non configurées.")
    
    try:
        requests.post(f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/restart", 
                     headers={"Authorization": f"Bearer {RENDER_API_KEY}"})
        await update.message.reply_text("🔄 Redémarrage du serveur Render lancé.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur redémarrage : {e}")

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
        
        df = await fetch_ohlcv_routed(symbol, tf, limit)
        trades, wins = 0, 0

        for i in range(100, len(df) - 2):
            ema10, p_ema10 = df['EMA10'].iloc[i], df['EMA10'].iloc[i-1]
            sma35, p_sma35 = df['SMA35'].iloc[i], df['SMA35'].iloc[i-1]
            ema55 = df['EMA55'].iloc[i]
            close = df['close'].iloc[i]

            long_sig = (p_ema10 <= p_sma35) and (ema10 > sma35) and (ema10 > ema55)
            short_sig = (p_ema10 >= p_sma35) and (ema10 < sma35) and (ema10 < ema55)

            if long_sig:
                eval_res = evaluate_setup(df, i, "BUY", "Backtest Long")
                if eval_res["status"] == "SIGNAL":
                    trades += 1
                    tp, sl = eval_res["tp"], eval_res["sl"]
                    for j in range(i+1, min(i+50, len(df))):
                        if df['high'].iloc[j] >= tp and df['low'].iloc[j] <= sl:
                            open_p = df['open'].iloc[j]
                            if abs(tp - open_p) < abs(open_p - sl):
                                wins += 1
                            break
                        elif df['high'].iloc[j] >= tp:
                            wins += 1; break
                        elif df['low'].iloc[j] <= sl:
                            break

            elif short_sig:
                eval_res = evaluate_setup(df, i, "SELL", "Backtest Short")
                if eval_res["status"] == "SIGNAL":
                    trades += 1
                    tp, sl = eval_res["tp"], eval_res["sl"]
                    for j in range(i+1, min(i+50, len(df))):
                        if df['low'].iloc[j] <= tp and df['high'].iloc[j] >= sl:
                            open_p = df['open'].iloc[j]
                            if abs(open_p - tp) < abs(sl - open_p):
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
    except Exception as e:
        await msg.edit_text(f"❌ Erreur : {e}")
