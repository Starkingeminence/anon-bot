import os
import asyncio
import random
from telethon import TelegramClient, events
from connection import Database
from users import handle_new_user
from fastest_fingers import handle_fastest_fingers
from qa import handle_qa_game
from voting import handle_vote
from leaderboard import update_leaderboard

# Environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Initialize database and Telegram client
db = Database(DATABASE_URL)
client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Permission helpers
async def is_owner(user_id, chat_id):
    # Check if the user is the owner of the chat
    # Implement your own logic
    ...

async def is_on_duty_admin(user_id, chat_id):
    # Check if user is an admin and on duty
    ...

async def off_duty_check(user_id, chat_id):
    if not await is_on_duty_admin(user_id, chat_id) and not await is_owner(user_id, chat_id):
        return True
    return False

# ---------- COMMAND HANDLERS ----------

@client.on(events.NewMessage(pattern=r"/raffle(?:\s+(.+))?"))
async def handle_raffle_command(event):
    chat_id = event.chat_id
    sender_id = event.sender_id
    args = event.pattern_match.group(1)

    if not await is_owner(sender_id, chat_id) and not await is_on_duty_admin(sender_id, chat_id):
        await event.reply("You must be an on-duty admin to use this command.")
        return

    # Fetch participants
    if args == "all":
        participants = await db.fetch_all_members(chat_id)
    elif args:
        # Usernames listed
        participants = args.split()
        random.shuffle(participants)
    else:
        await event.reply("Usage: /raffle all OR /raffle username1 username2 ...")
        return

    # Exclude dropped/off-duty admins
    participants = [p for p in participants if not await off_duty_check(p, chat_id)]

    if not participants:
        await event.reply("No eligible participants found.")
        return

    # Simulate raffle drawing effect
    msg = await event.reply("Picking a winner...")
    for i in range(3):
        await asyncio.sleep(1)
        await msg.edit(f"Processing{'.'* (i+1)}")
    
    winner = random.choice(participants)
    await msg.edit(f"🎉 Congratulations {winner}! You won the raffle!")

@client.on(events.NewMessage)
async def on_new_message(event):
    chat_id = event.chat_id
    sender_id = event.sender_id
    message = event.message.message

    # Off-duty admin check for general commands (not voting)
    if await off_duty_check(sender_id, chat_id):
        return  # ignore everyday commands

    # Example calls to your game/voting functions
    if message.startswith("/qa"):
        # expects answer argument
        parts = message.split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("Please provide an answer.")
        else:
            answer = parts[1]
            await handle_qa_game(sender_id, chat_id, message, answer)
    
    elif message.startswith("/fastest"):
        await handle_fastest_fingers(sender_id, chat_id, message)

    elif message.startswith("/vote"):
        await handle_vote(sender_id, chat_id, message)

# ---------- MAIN FUNCTION ----------

async def main():
    print(f"DATABASE_URL: {DATABASE_URL}")
    try:
        await db.connect()
        print("Database connected ✅")
    except Exception as e:
        print("Database connection failed ❌:", e)
        return

    print("Telegram client started")
    await client.run_until_disconnected()

# Entry point
if __name__ == "__main__":
    asyncio.run(main())
