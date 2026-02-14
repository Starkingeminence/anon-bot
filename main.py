import os
import asyncio
import logging

from telegram.ext import ApplicationBuilder

from connection import db

# PTB modules
from utils import register_utils_handlers
from economy import register_economy_handlers
from governance import register_governance_handlers
from games import register_games_handlers
from moderation import register_moderation_handlers
from analytics import register_analytics_handlers, referral_scheduler

# Telethon anon client
from anon_messaging import start_anon_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not DATABASE_URL or not BOT_TOKEN:
    raise SystemExit("Missing DATABASE_URL or TELEGRAM_BOT_TOKEN")


async def main():

    # -----------------------
    # Connect DB
    # -----------------------
    await db.connect(DATABASE_URL)
    logger.info("Database connected ✅")

    # -----------------------
    # Build PTB Bot
    # -----------------------
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register feature modules
    register_utils_handlers(app)
    register_economy_handlers(app)
    register_governance_handlers(app)
    register_games_handlers(app)
    register_moderation_handlers(app)
    register_analytics_handlers(app)

    logger.info("Handlers registered ✅")

    # -----------------------
    # Start Background Tasks
    # -----------------------
    asyncio.create_task(referral_scheduler(app))
    asyncio.create_task(start_anon_client())

    logger.info("Background services started ✅")

    # -----------------------
    # Run PTB
    # -----------------------
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
