from telethon import TelegramClient
from config import API_ID, API_HASH

# Create client ONLY
# Do NOT start it here
bot_client = TelegramClient(
    "bot_session",
    API_ID,
    API_HASH
)
