import asyncio
from client import bot
from database.connection import db

# Import your modules here to register handlers
# Example placeholders
from core import permissions, groups, users
from moderation import captcha, blacklist, language
from governance import voting, weights
from payments import tiers, ton, grace
from dm_features import personal_votes
from games import fastest_fingers, qa, leaderboard
from utils import telegram, time, text

async def main():
    """
    Main entry point for the bot.
    Connects to the database, starts the bot,
    and keeps it running until disconnected.
    """
    # Connect to PostgreSQL
    await db.connect()

    # Here you can initialize any tables if needed
    # from database.models import create_tables
    # await create_tables(db.pool)

    print("All modules loaded. Bot is starting...")

    try:
        # Start the bot and run until disconnected
        await bot.run_until_disconnected()
    finally:
        # Ensure DB disconnects cleanly on shutdown
        await db.close()
        print("Bot stopped.")

if __name__ == "__main__":
    # Run the async main loop
    asyncio.run(main())
