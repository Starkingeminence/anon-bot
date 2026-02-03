import asyncio
from telethon import events

import config
from client import bot_client
from connection import db
from groups import handle_new_group
from users import handle_new_user
from voting import handle_vote
from captcha import verify_captcha
from fastest_fingers import handle_fastest_fingers
from qa import handle_qa_game
from leaderboard import update_leaderboard


# -------------------------------------
# Start Bot
# -------------------------------------

async def main():
    print("Bot starting...")

    # Connect to DB
    await db.connect()
    print("Database connected")

    # Start the Telethon client
    await bot_client.start(bot_token=config.BOT_TOKEN)
    print("Telegram client started")

    # Listen for new messages
    @bot_client.on(events.NewMessage)
    async def on_new_message(event):
        chat_id = event.chat_id
        sender_id = event.sender_id
        message = event.message.message or ""

        # Handle group initialization
        await handle_new_group(chat_id)

        # Handle user registration
        await handle_new_user(sender_id, chat_id)

        # Captcha verification
        await verify_captcha(sender_id, chat_id, message)

        # Games
        if config.ENABLE_GAMES:
            await handle_fastest_fingers(sender_id, chat_id, message)
            await handle_qa_game(sender_id, chat_id, message)

        # Voting
        if config.ENABLE_DM_VOTES:
            await handle_vote(sender_id, chat_id, message)

        # Leaderboard update
        await update_leaderboard(chat_id)

    print("Bot is now listening for events...")
    await bot_client.run_until_disconnected()


# -------------------------------------
# Entry point
# -------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
