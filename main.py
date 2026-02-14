import os
import asyncio
import logging

from telegram.ext import ApplicationBuilder

from connection import db

# Feature modules
from utils import register_utils_handlers
from economy import register_economy_handlers
from governance import register_governance_handlers
from games import register_games_handlers
from moderation import register_moderation_handlers
from analytics import (
    register_analytics_handlers,
    referral_scheduler,
)

# --------------------------------
# Logging
# --------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------
# Environment Variables
# --------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not DATABASE_URL or not BOT_TOKEN:
    raise SystemExit("Missing DATABASE_URL or TELEGRAM_BOT_TOKEN")

# --------------------------------
# Main
# --------------------------------

async def main():

    # --------------------
    # Connect Database
    # --------------------
    try:
        await db.connect(DATABASE_URL)
        logger.info("Database connected ✅")
    except Exception as e:
        logger.error(f"Database connection failed ❌: {e}")
        raise

    # --------------------
    # Build Bot App
    # --------------------
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --------------------
    # Register Modules
    # --------------------
    register_utils_handlers(app)
    register_economy_handlers(app)
    register_governance_handlers(app)
    register_games_handlers(app)
    register_moderation_handlers(app)
    register_analytics_handlers(app)

    logger.info("All handlers registered ✅")

    # --------------------
    # Background Tasks
    # --------------------
    asyncio.create_task(referral_scheduler(app))
    logger.info("Referral scheduler started (5 min loop)")

    # --------------------
    # Start Bot
    # --------------------
    logger.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.idle()


# --------------------------------
# Entrypoint
# --------------------------------

if __name__ == "__main__":
    asyncio.run(main())
