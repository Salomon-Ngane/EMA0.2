import asyncio
from database.connection import get_supabase

async def get_user_profile(telegram_id: int):
    db = get_supabase()
    if not db:
        return None
    try:
        res = await asyncio.to_thread(lambda: db.table("users").select("*").eq("telegram_id", telegram_id).execute())
        if res.data:
            return res.data[0]
    except Exception as e:
        print(f"Erreur get_user_profile: {e}")
    return None

async def upsert_user(telegram_id: int, username: str, role: str, max_assets: int):
    db = get_supabase()
    if not db:
        return None
    try:
        data = {
            "telegram_id": telegram_id,
            "username": username,
            "role": role,
            "max_assets": max_assets
        }
        res = await asyncio.to_thread(lambda: db.table("users").upsert(data).execute())
        return res
    except Exception as e:
        print(f"Erreur upsert_user: {e}")
        return None
