import os
import asyncio
from telethon import TelegramClient, events
from connection import Database
from aiohttp import web

# --------------------------
# CONFIG
# --------------------------
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8000))  # Render provides this

# --------------------------
# DATABASE
# --------------------------
db = Database()

# --------------------------
# TELEGRAM CLIENT
# --------------------------
client = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)


# --------------------------
# HANDLERS
# --------------------------
@client.on(events.NewMessage)
async def handle_new_message(event):
    sender = await event.get_sender()
    chat = await event.get_chat()
    message = event.message.message

    sender_id = sender.id
    chat_id = chat.id

    if message.startswith("/raffle"):
        await handle_raffle_command(chat_id, sender_id, message)


async def handle_raffle_command(chat_id, sender_id, message):
    await client.send_message(chat_id, "🎉 Raffle is being processed...")
    await asyncio.sleep(1)
    await client.send_message(chat_id, "🎲 Picking winner...")
    await asyncio.sleep(1)
    await client.send_message(chat_id, "🏆 Winner: @example_user")


# --------------------------
# SIMPLE WEB SERVER TO KEEP RENDER HAPPY
# --------------------------
async def handle_root(request):
    return web.Response(text="Bot is running!")

async def start_web_app():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"Web service listening on port {PORT}")


# --------------------------
# MAIN FUNCTION
# --------------------------
async def main():
    print(f"DATABASE_URL: {DATABASE_URL}")
    try:
        await db.connect(DATABASE_URL)
        print("Database connected ✅")
    except Exception as e:
        print(f"Database connection failed ❌: {e}")

    await start_web_app()       # Keeps Render happy
    await client.run_until_disconnected()  # Runs Telegram client


# --------------------------
# ENTRY POINT
# --------------------------
if __name__ == "__main__":
    asyncio.run(main())
