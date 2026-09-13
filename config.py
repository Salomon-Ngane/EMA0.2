import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID")
RENDER_API_KEY = os.getenv("RENDER_API_KEY")

ADMIN_USERNAME = "@ideasanddreams"
ADMIN_ID = 1096334202
PORT = int(os.getenv("PORT", 8080))

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
PIVOT_LEFT_RIGHT = 10
