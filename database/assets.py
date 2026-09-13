import asyncio
from database.connection import get_supabase

async def get_all_assets():
    db = get_supabase()
    if not db:
        return []
    try:
        res = await asyncio.to_thread(lambda: db.table("assets").select("*").execute())
        return res.data if res.data else []
    except Exception as e:
        print(f"Erreur get_all_assets: {e}")
        return []

async def get_user_assets(telegram_id: int):
    db = get_supabase()
    if not db:
        return []
    try:
        res = await asyncio.to_thread(lambda: db.table("assets").select("*").eq("telegram_id", telegram_id).execute())
        return res.data if res.data else []
    except Exception as e:
        print(f"Erreur get_user_assets: {e}")
        return []

async def add_asset(telegram_id: int, symbol: str, timeframe: str):
    db = get_supabase()
    if not db:
        return None
    try:
        data = {"telegram_id": telegram_id, "symbol": symbol, "timeframe": timeframe}
        res = await asyncio.to_thread(lambda: db.table("assets").insert(data).execute())
        return res
    except Exception as e:
        print(f"Erreur add_asset: {e}")
        return None

async def remove_asset(telegram_id: int, symbol: str):
    db = get_supabase()
    if not db:
        return None
    try:
        res = await asyncio.to_thread(lambda: db.table("assets").delete().eq("telegram_id", telegram_id).eq("symbol", symbol).execute())
        return res
    except Exception as e:
        print(f"Erreur remove_asset: {e}")
        return None

async def update_asset_tf(telegram_id: int, symbol: str, timeframe: str):
    db = get_supabase()
    if not db:
        return None
    try:
        res = await asyncio.to_thread(lambda: db.table("assets").update({"timeframe": timeframe}).eq("telegram_id", telegram_id).eq("symbol", symbol).execute())
        return res
    except Exception as e:
        print(f"Erreur update_asset_tf: {e}")
        return None
