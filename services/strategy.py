import pandas as pd
from config import PIVOT_LEFT_RIGHT

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
                if (df['low'].iloc[p] < df['EMA10'].iloc[p] and 
                    df['low'].iloc[p] < df['SMA35'].iloc[p] and 
                    df['low'].iloc[p] < df['EMA55'].iloc[p]):
                    return df['low'].iloc[p]
            elif condition == "ABOVE_EMAS":
                if (df['high'].iloc[p] > df['EMA10'].iloc[p] and 
                    df['high'].iloc[p] > df['SMA35'].iloc[p] and 
                    df['high'].iloc[p] > df['EMA55'].iloc[p]):
                    return df['high'].iloc[p]
            else:
                return df['high'].iloc[p] if kind == "HIGH" else df['low'].iloc[p]
    
    lookback_start = max(0, current_idx - 50)
    return df['high'].iloc[lookback_start:current_idx].max() if kind == "HIGH" else df['low'].iloc[lookback_start:current_idx].min()

def evaluate_setup(df: pd.DataFrame, i: int, direction: str, signal_type: str) -> dict:
    """
    Point d'entrée UNIQUE pour valider un setup (Cassure ou Retest).
    Calcule SL/TP via les pivots et applique le filtre R:R >= 2.7.
    """
    close = df['close'].iloc[i]
    if direction == "BUY":
        tp = get_last_pivot(df, i, window=PIVOT_LEFT_RIGHT, kind="HIGH")
        sl = get_last_pivot(df, i, window=PIVOT_LEFT_RIGHT, kind="LOW", condition="BELOW_EMAS")
        valid_geometry = tp > close and sl < close
        rr = (tp - close) / (close - sl) if valid_geometry else None
    else:
        tp = get_last_pivot(df, i, window=PIVOT_LEFT_RIGHT, kind="LOW")
        sl = get_last_pivot(df, i, window=PIVOT_LEFT_RIGHT, kind="HIGH", condition="ABOVE_EMAS")
        valid_geometry = tp < close and sl > close
        rr = (close - tp) / (sl - close) if valid_geometry else None

    if not valid_geometry or rr is None:
        return {"status": "NONE", "reason": "Géométrie SL/TP invalide"}
    if rr < 2.7:
        return {"status": "REJECTED", "reason": f"R:R = {rr:.2f} < 2.7", "rr": rr, "tp": tp, "sl": sl}
    
    return {
        "status": "SIGNAL",
        "signal_type": signal_type,
        "direction": direction,
        "entry": close,
        "tp": tp,
        "sl": sl,
        "rr": rr
    }

def analyze_market(df: pd.DataFrame, symbol: str, timeframe: str, rejected_logs=None) -> dict:
    if len(df) < 60:
        return {"status": "NONE", "msg": "⚪ Historique insuffisant"}
    
    i = len(df) - 2
    last = df.iloc[i]
    prev = df.iloc[i-1]
    
    ema10, prev_ema10 = last['EMA10'], prev['EMA10']
    sma35, prev_sma35 = last['SMA35'], prev['SMA35']
    ema55, prev_ema55 = last['EMA55'], prev['EMA55']
    
    long_signal = (prev_ema10 <= prev_sma35) and (ema10 > sma35) and (ema10 > ema55)
    short_signal = (prev_ema10 >= prev_sma35) and (ema10 < sma35) and (ema10 < ema55)
    
    if long_signal:
        eval_res = evaluate_setup(df, i, "BUY", "Cassure Achat")
        if eval_res["status"] == "SIGNAL":
            return {"status": "SIGNAL", "msg": f"🚀 <b>SIGNAL ACHAT (Cassure)</b>\n🔹 Entrée: {eval_res['entry']}\n🎯 TP: {eval_res['tp']:.4f}\n🛑 SL: {eval_res['sl']:.4f}\n⚖️ R:R: {eval_res['rr']:.2f}"}
        elif eval_res["status"] == "REJECTED":
            if rejected_logs is not None:
                rejected_logs.appendleft(f"[{symbol} {timeframe}] ACHAT Cassure rejeté | {eval_res['reason']}")
            return {"status": "NONE", "msg": f"🟢 Tendance Haussière (Signal écarté {eval_res['reason']})"}
            
    elif short_signal:
        eval_res = evaluate_setup(df, i, "SELL", "Cassure Vente")
        if eval_res["status"] == "SIGNAL":
            return {"status": "SIGNAL", "msg": f"⚠️ <b>SIGNAL VENTE (Cassure)</b>\n🔹 Entrée: {eval_res['entry']}\n🎯 TP: {eval_res['tp']:.4f}\n🛑 SL: {eval_res['sl']:.4f}\n⚖️ R:R: {eval_res['rr']:.2f}"}
        elif eval_res["status"] == "REJECTED":
            if rejected_logs is not None:
                rejected_logs.appendleft(f"[{symbol} {timeframe}] VENTE Cassure rejetée | {eval_res['reason']}")
            return {"status": "NONE", "msg": f"🔴 Tendance Baissière (Signal écarté {eval_res['reason']})"}

    # Retest Logic utilisant le même evaluate_setup
    if ema10 > sma35 and ema10 > ema55:
        recent_cross = any((df['EMA10'].iloc[k-1] <= df['SMA35'].iloc[k-1] and df['EMA10'].iloc[k] > df['SMA35'].iloc[k]) for k in range(i, max(0, i - 15), -1))
        if recent_cross and last['low'] <= ema10 and prev['low'] > prev['EMA10'] and last['close'] > sma35 and last['close'] > ema55:
            eval_res = evaluate_setup(df, i, "BUY", "Retest Achat")
            if eval_res["status"] == "SIGNAL":
                return {"status": "SIGNAL", "msg": f"🔄 <b>RETEST ACHAT (Validé)</b>\n🔹 Prix: {eval_res['entry']}\n🎯 TP: {eval_res['tp']:.4f}\n🛑 SL: {eval_res['sl']:.4f}\n⚖️ R:R: {eval_res['rr']:.2f}"}
            elif eval_res["status"] == "REJECTED":
                if rejected_logs is not None:
                    rejected_logs.appendleft(f"[{symbol} {timeframe}] RETEST ACHAT rejeté | {eval_res['reason']}")
                return {"status": "NONE", "msg": f"🟢 Retest Achat écarté ({eval_res['reason']})"}
        return {"status": "NONE", "msg": "🟢 Tendance Haussière"}
        
    elif ema10 < sma35 and ema10 < ema55:
        recent_cross = any((df['EMA10'].iloc[k-1] >= df['SMA35'].iloc[k-1] and df['EMA10'].iloc[k] < df['SMA35'].iloc[k]) for k in range(i, max(0, i - 15), -1))
        if recent_cross and last['high'] >= ema10 and prev['high'] < prev['EMA10'] and last['close'] < sma35 and last['close'] < ema55:
            eval_res = evaluate_setup(df, i, "SELL", "Retest Vente")
            if eval_res["status"] == "SIGNAL":
                return {"status": "SIGNAL", "msg": f"🔄 <b>RETEST VENTE (Validé)</b>\n🔹 Prix: {eval_res['entry']}\n🎯 TP: {eval_res['tp']:.4f}\n🛑 SL: {eval_res['sl']:.4f}\n⚖️ R:R: {eval_res['rr']:.2f}"}
            elif eval_res["status"] == "REJECTED":
                if rejected_logs is not None:
                    rejected_logs.appendleft(f"[{symbol} {timeframe}] RETEST VENTE rejeté | {eval_res['reason']}")
                return {"status": "NONE", "msg": f"🔴 Retest Vente écarté ({eval_res['reason']})"}
        return {"status": "NONE", "msg": "🔴 Tendance Baissière"}
        
    return {"status": "NONE", "msg": "⚪ Neutre (Structure non alignée)"}
