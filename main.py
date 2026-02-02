import os
from telethon import TelegramClient, events
from config import Config
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
    await bot_client.start()
    print("Telegram client started")
    
    # Listen for new messages
    @bot_client.on(events.NewMessage)
    async def on_new_message(event):
        chat_id = event.chat_id
        sender = event.sender_id
        message = event.message.message
        
        # Handle new groups or join events
        await handle_new_group(chat_id)
        
        # Handle user registration / first-time joining
        await handle_new_user(sender, chat_id)
        
        # Check captcha if needed
        await verify_captcha(sender, chat_id, message)
        
        # Check for commands related to games
        await handle_fastest_fingers(sender, chat_id, message)
        await handle_qa_game(sender, chat_id, message)
        
        # Check voting commands
        await handle_vote(sender, chat_id, message)
        
        # Update leaderboard if necessary
        await update_leaderboard(chat_id)

    print("Bot is now listening for events...")
    await bot_client.run_until_disconnected()


# -------------------------------------
# Entry point
# -------------------------------------
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
