import re
import unicodedata
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Callable, Coroutine
from functools import wraps

from telethon import TelegramClient, Button

logger = logging.getLogger(__name__)

# ==========================================================
# TEXT UTILITIES
# ==========================================================
def normalize_text(text: str) -> str:
    """
    Normalize text by:
    - Removing accents
    - Converting to lowercase
    - Stripping spaces
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("utf-8")
    return text.lower().strip()

def remove_emojis(text: str) -> str:
    """
    Remove emojis from text.
    """
    emoji_pattern = re.compile(
        "[" 
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text)

def remove_links(text: str) -> str:
    """
    Remove URLs from text.
    """
    url_pattern = re.compile(r"http\S+|www\S+")
    return url_pattern.sub("", text).strip()

# ==========================================================
# TIME UTILITIES
# ==========================================================
def now_utc() -> datetime:
    return datetime.utcnow()

def days_ago(days: int) -> datetime:
    return now_utc() - timedelta(days=days)

def format_datetime(dt: datetime) -> str:
    """Return a UTC timestamp string"""
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

def seconds_until(target_time: datetime) -> int:
    delta = target_time - now_utc()
    return max(int(delta.total_seconds()), 0)

# ==========================================================
# TELEGRAM UTILITIES (Telethon-based)
# ==========================================================
def safe_telegram_action(func: Callable[..., Coroutine]):
    """
    Decorator to wrap Telegram actions with error handling and logging.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            chat_repr = kwargs.get("chat_id") or kwargs.get("user_id") or "unknown"
            logger.error(f"Telegram action {func.__name__} failed for {chat_repr}: {e}")
            return None
    return wrapper

@safe_telegram_action
async def send_dm(
    bot: TelegramClient,
    user_id: int,
    message: str,
    buttons: Optional[List[Button]] = None,
    display_name: Optional[str] = None
):
    """
    Send private message to a user.
    display_name is optional for logging instead of raw ID.
    """
    if buttons:
        await bot.send_message(user_id, message, buttons=buttons)
    else:
        await bot.send_message(user_id, message)
    logger.info(f"Sent DM to {display_name or user_id}")

@safe_telegram_action
async def send_group_message(
    bot: TelegramClient,
    group_id: int,
    message: str,
    buttons: Optional[List[Button]] = None
):
    if buttons:
        await bot.send_message(group_id, message, buttons=buttons)
    else:
        await bot.send_message(group_id, message)
    logger.info(f"Sent message to group {group_id}")

@safe_telegram_action
async def send_buttons(
    bot: TelegramClient,
    chat_id: int,
    message: str,
    button_list: List[List[Button]]
):
    await bot.send_message(chat_id, message, buttons=button_list)
    logger.info(f"Sent buttons to chat {chat_id}")

@safe_telegram_action
async def edit_message(
    bot: TelegramClient,
    chat_id: int,
    message_id: int,
    new_text: str,
    buttons: Optional[List[Button]] = None
):
    if buttons:
        await bot.edit_message(chat_id, message_id, text=new_text, buttons=buttons)
    else:
        await bot.edit_message(chat_id, message_id, text=new_text)
    logger.info(f"Edited message {message_id} in chat {chat_id}")

@safe_telegram_action
async def delete_message(
    bot: TelegramClient,
    chat_id: int,
    message_id: int
):
    await bot.delete_messages(chat_id, message_id)
    logger.info(f"Deleted message {message_id} in chat {chat_id}")

def register_utils_handlers(app):
    """Placeholder for utility command registration. Currently does nothing."""
    pass
