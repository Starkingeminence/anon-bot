import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telethon API credentials
API_ID = int(os.getenv("API_ID", 0))          # Telegram API ID
API_HASH = os.getenv("API_HASH", "")          # Telegram API hash
BOT_TOKEN = os.getenv("BOT_TOKEN", "")        # Bot token from BotFather

# PostgreSQL database URL
DATABASE_URL = os.getenv("DATABASE_URL", "")  # e.g., postgresql://user:password@host:port/dbname

# General bot settings
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")   # Can be used for timestamps
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")                # Logging level

# Optional: feature flags or defaults
ENABLE_GAMES = os.getenv("ENABLE_GAMES", "True") == "True"
ENABLE_DM_VOTES = os.getenv("ENABLE_DM_VOTES", "True") == "True"

# Validation checks
if API_ID == 0 or not API_HASH or not BOT_TOKEN or not DATABASE_URL:
    raise ValueError(
        "One or more required environment variables are missing: "
        "API_ID, API_HASH, BOT_TOKEN, DATABASE_URL"
    )
