import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# -----------------------------
# Telegram credentials
# -----------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# -----------------------------
# Database
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")

# -----------------------------
# TON Blockchain Wallet & API
# -----------------------------
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "")
TON_CENTER_API_KEY = os.getenv("TON_CENTER_API_KEY", "")

# -----------------------------
# Bot configuration
# -----------------------------
DEFAULT_TIER = os.getenv("DEFAULT_TIER", "free")
MAX_CAPTCHA_TRIES = int(os.getenv("MAX_CAPTCHA_TRIES", 5))
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", 15))
MIN_GRACE_DAYS = int(os.getenv("MIN_GRACE_DAYS", 1))

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# -----------------------------
# Validation (fail fast)
# -----------------------------
if not API_ID:
    raise RuntimeError("API_ID is missing")

if not API_HASH:
    raise RuntimeError("API_HASH is missing")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if not TON_WALLET_ADDRESS:
    raise RuntimeError("TON_WALLET_ADDRESS is missing")

if not TON_CENTER_API_KEY:
    raise RuntimeError("TON_CENTER_API_KEY is missing")
