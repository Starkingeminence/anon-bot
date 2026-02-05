import os
import asyncio
from telethon import TelegramClient, events
from connection import Database

# -------------------------------
# Configuration
# -------------------------------
API_ID = int(os.getenv("API_ID"))           # Your Telegram API ID
API_HASH = os.getenv("API_HASH")           # Your Telegram API hash
BOT_TOKEN = os.getenv("BOT_TOKEN")         # Bot token from @BotFather
DATABASE_URL = os.getenv("DATABASE_URL")   # Postgres connection string

# -------------------------------
# Initialize database and client
# -------------------------------
db = Database()  # We'll pass DATABASE_URL in connect() instead of __init__
client = TelegramClient("bot_session", API_ID, API_HASH)

# -------------------------------
# Example handlers
# -------------------------------

@client.on(events.NewMessage)
async def on_new_message(event):
    chat_id = event.chat_id
    sender_id = event.sender_id
    message = event.message.message

    # Example: simple echo
    if message.startswith("/echo "):
        await event.reply(message[6:])

    # TODO: call your game/raffle handlers here
    # Example:
    # await handle_qa_game(sender_id, chat_id, message)
    # await handle_raffle(sender_id, chat_id, message)

# -------------------------------
# Main function
# -------------------------------
async def main():
    # Connect to the database
    try:
        await db.connect(DATABASE_URL)
        print("Database connected ✅")
    except Exception as e:
        print("Database connection failed ❌:", e)

    # Start Telegram bot
    await client.start(bot_token=BOT_TOKEN)
    print("Telegram client started")
    print("Bot is now running...")

    # Keep the bot running
    await client.run_until_disconnected()

# -------------------------------
# Entry point
# -------------------------------
if __name__ == "__main__":
    # Create a single event loop for everything
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(client.disconnect())
        loop.close()
    print("Telegram client started")
    await client.run_until_disconnected()

# Entry point
if __name__ == "__main__":
    asyncio.run(main())
