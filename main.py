import asyncio
from telethon import events

from config import BOT_TOKEN
from client import bot_client
from connection import db

from groups import handle_new_group
from users import handle_new_user
from captcha import verify_captcha
from fastest_fingers import handle_fastest_fingers
from qa import handle_qa_game
from voting import handle_vote
from leaderboard import update_leaderboard


async def main():
    print("Bot starting...")

    # Connect database
    await db.connect()
    print("Database connected")

    # Start Telegram bot (ONLY place BOT_TOKEN is used)
    await bot_client.start(bot_token=BOT_TOKEN)
    print("Telegram client started")

    @bot_client.on(events.NewMessage)
    async def on_new_message(event):
        chat_id = event.chat_id
        sender_id = event.sender_id
        message = event.raw_text or ""

        await handle_new_group(chat_id)
        await handle_new_user(sender_id, chat_id)
        await verify_captcha(sender_id, chat_id, message)

        await handle_fastest_fingers(sender_id, chat_id, message)
        await handle_qa_game(sender_id, chat_id, message)
        await handle_vote(sender_id, chat_id, message)

        await update_leaderboard(chat_id)

    print("Bot is now running...")
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
