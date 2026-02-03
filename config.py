import os
from dotenv import load_dotenv

load_dotenv()

# Telegram credentials
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Basic validation
if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Missing required Telegram environment variables")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
