# utils.py
import re
import unicodedata
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from telethon import TelegramClient, Button

logger = logging.getLogger(__name__)

# ==========================
# TEXT HELPERS
# ==========================

def normalize_text(text: str) -> str:
    """Normalize text: remove accents, lowercase, strip spaces."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("utf-8")
    text = text.lower().strip()
    return text

def remove_emojis(text: str) -> str:
    """Remove emojis from a string."""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002700-\U000027BF"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", text)

def remove_links(text: str) -> str:
    """Remove URLs from a string."""
    url_pattern = re.compile(r"https?://\S+|www\.\S+")
    return url_pattern.sub(r"", text)

def shorten_text(text: str, max_length: int = 50) -> str:
    """Shorten text with ellipsis if longer than max_length."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."

# ==========================
# TIME HELPERS
# ==========================

def now_utc() -> datetime:
    return datetime.utcnow()

def format_seconds(seconds: int) -> str:
    """Convert seconds to 1h 1m 1s format."""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if secs or not parts: parts.append(f"{secs}s")
    return " ".join(parts)

def format_timedelta(td: timedelta) -> str:
    return format_seconds(int(td.total_seconds()))

def seconds_until(future_time: datetime) -> int:
    delta = future_time - now_utc()
    return max(int(delta.total_seconds()), 0)

# ==========================
# TELEGRAM HELPERS
# ==========================

async def send_dm(bot: TelegramClient, user_id: int, message: str, buttons: Optional[List[Button]] = None):
    try:
        await bot.send_message(user_id, message, buttons=buttons)
        logger.info(f"Sent DM to {user_id}")
    except Exception as e:
        logger.error(f"Failed to send DM to {user_id}: {e}")

async def send_group_message(bot: TelegramClient, group_id: int, message: str, buttons: Optional[List[Button]] = None):
    try:
        await bot.send_message(group_id, message, buttons=buttons)
        logger.info(f"Sent message to group {group_id}")
    except Exception as e:
        logger.error(f"Failed to send message to group {group_id}: {e}")

async def delete_message(bot: TelegramClient, chat_id: int, message_id: int):
    try:
        await bot.delete_messages(chat_id, message_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")
