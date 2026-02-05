import os
import asyncio
import logging

from telethon import TelegramClient, events
from telethon.tl import functions

from connection import db  # Ensure your connection.py exposes `db` correctly

# ----------------------
# Logging setup
# ----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------
# Environment variables
# ----------------------
DATABASE_URL = os.getenv("DATABASE_URL")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_PORT = int(os.getenv("PORT", 10000))  # Render default port

if not all([DATABASE_URL, API_ID, API_HASH, BOT_TOKEN]):
    logger.error("Missing one or more required environment variables!")
    raise SystemExit("Check DATABASE_URL, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")

# ----------------------
# Telegram client
# ----------------------
client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ----------------------
# Event handlers
# ----------------------
@client.on(events.NewMessage)
async def on_new_message(event):
    sender = await event.get_sender()
    chat = await event.get_chat()
    message = event.message.message

    # Example: simple reply
    await event.respond("Bot received your message!")

    # TODO: call your game / raffle handlers here
    # await handle_qa_game(sender.id, chat.id, message)

# ----------------------
# Main async entrypoint
# ----------------------
async def main():
    logger.info(f"DATABASE_URL: {DATABASE_URL}")

    # Connect to DB
    try:
        await db.connect(DATABASE_URL)
        logger.info("Database connected ✅")
    except Exception as e:
        logger.error(f"Database connection failed ❌: {e}")

    # Start Telegram client
    await client.start()
    logger.info("Telegram client started")

    # If you have any background tasks, start them here

    # Keep the bot running
    await client.run_until_disconnected()

# ----------------------
# Entrypoint
# ----------------------
if __name__ == "__main__":
    # Handle Render’s event loop issues
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            asyncio.ensure_future(main())
        else:
            asyncio.run(main())
    except RuntimeError:
        asyncio.run(main())
