import os
import asyncio
import logging
from telegram.ext import ApplicationBuilder

from connection import db
from utils import register_utils_handlers
from economy import register_economy_handlers, subscription_phase_watcher
from games import register_games_handlers
from moderation import register_moderation_handlers
from analytics import register_analytics_handlers, referral_scheduler
from anon_messaging import start_anon_client

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


async def safe_task(coro, name):
    try:
        await coro
    except Exception:
        logger.exception(f"Background task crashed: {name}")


async def post_init(app):
    # Connect DB
    await db.connect(DATABASE_URL)
    logger.info("Database connected ✅")

    # Start background tasks
    asyncio.create_task(safe_task(referral_scheduler(app), "referral_scheduler"))
    asyncio.create_task(safe_task(subscription_phase_watcher(app), "phase_watcher"))
    asyncio.create_task(safe_task(start_anon_client(), "anon_client"))
    logger.info("Background services started ✅")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Register handlers
    register_utils_handlers(app)
    register_economy_handlers(app)
    register_games_handlers(app)
    register_moderation_handlers(app)
    register_analytics_handlers(app)
    logger.info("Handlers registered ✅")

    # IMPORTANT: no asyncio.run(), no await, no initialize()
    app.run_polling()


if __name__ == "__main__":
    main()
