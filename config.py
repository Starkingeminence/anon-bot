import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Telegram credentials
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# General settings
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Feature flags
ENABLE_GAMES = os.getenv("ENABLE_GAMES", "True").lower() == "true"
ENABLE_DM_VOTES = os.getenv("ENABLE_DM_VOTES", "True").lower() == "true"

# Validation
missing = []
if API_ID == 0:
    missing.append("API_ID")
if not API_HASH:
    missing.append("API_HASH")
if not BOT_TOKEN:
    missing.append("BOT_TOKEN")
if not DATABASE_URL:
    missing.append("DATABASE_URL")

if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}"
    )
