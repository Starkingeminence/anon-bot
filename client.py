import logging
from telethon import TelegramClient, events
from config import API_ID, API_HASH, BOT_TOKEN

# ---------------------------
# Configure logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------------
# Initialize Telethon client
# ---------------------------
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

logger.info("Telethon client initialized")

# ---------------------------
# Example command /start
# ---------------------------
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.respond(
        "👋 Hello! I am your multi-feature bot.\n"
        "All features will be available depending on your role and group settings."
    )
    logger.info(f"/start command used by {event.sender_id}")

# ---------------------------
# Function to register all module handlers
# ---------------------------
def register_handlers():
    """
    Import modules here so their handlers are automatically registered
    with the Telethon client.
    Example:
        from moderation import captcha, blacklist
        from games import fastest_fingers, qa
    """
    logger.info("Registering handlers from all modules")
    # This is where each module with @bot.on(...) gets imported
    # e.g., import moderation.captcha, import games.qa

# ---------------------------
# Optional helper: start client
# ---------------------------
async def start_bot():
    register_handlers()
    logger.info("Starting bot...")
    await bot.run_until_disconnected()
