import re
import unicodedata
import logging
from datetime import datetime, timedelta
from typing import List, Optional

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
    return url_pattern.sub("", text)


# ==========================================================
# TIME UTILITIES
# ==========================================================

def now_utc() -> datetime:
    """
    Return current UTC time.
    """
    return datetime.utcnow()


def days_ago(days: int) -> datetime:
    """
    Return datetime for X days ago.
    """
    return now_utc() - timedelta(days=days)


def format_datetime(dt: datetime) -> str:
    """
    Format datetime for display.
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def seconds_until(target_time: datetime) -> int:
    """
    Returns number of seconds until target_time.
    """
    delta = target_time - now_utc()
    return max(int(delta.total_seconds()), 0)


# ==========================================================
# TELEGRAM UTILITIES (Telethon-based)
# ==========================================================

async def send_dm(
    bot: TelegramClient,
    user_id: int,
    message: str,
    buttons: Optional[List[Button]] = None
):
    """
    Send private message to a user.
    """
    try:
        if buttons:
            await bot.send_message(user_id, message, buttons=buttons)
        else:
            await bot.send_message(user_id, message)

        logger.info(f"Sent DM to {user_id}")

    except Exception as e:
        logger.error(f"Failed to send DM to {user_id}: {e}")


async def send_group_message(
    bot: TelegramClient,
    group_id: int,
    message: str,
    buttons: Optional[List[Button]] = None
):
    """
    Send message to group or channel.
    """
    try:
        if buttons:
            await bot.send_message(group_id, message, buttons=buttons)
        else:
            await bot.send_message(group_id, message)

        logger.info(f"Sent message to group {group_id}")

    except Exception as e:
        logger.error(f"Failed to send message to group {group_id}: {e}")


async def send_buttons(
    bot: TelegramClient,
    chat_id: int,
    message: str,
    button_list: List[List[Button]]
):
    """
    Send message with inline buttons arranged in rows.
    """
    try:
        await bot.send_message(chat_id, message, buttons=button_list)
        logger.info(f"Sent buttons to {chat_id}")

    except Exception as e:
        logger.error(f"Failed to send buttons to {chat_id}: {e}")


async def edit_message(
    bot: TelegramClient,
    chat_id: int,
    message_id: int,
    new_text: str,
    buttons: Optional[List[Button]] = None
):
    """
    Edit a previously sent message.
    """
    try:
        if buttons:
            await bot.edit_message(chat_id, message_id, text=new_text, buttons=buttons)
        else:
            await bot.edit_message(chat_id, message_id, text=new_text)

        logger.info(f"Edited message {message_id} in chat {chat_id}")

    except Exception as e:
        logger.error(f"Failed to edit message {message_id} in chat {chat_id}: {e}")


async def delete_message(
    bot: TelegramClient,
    chat_id: int,
    message_id: int
):
    """
    Delete message from chat.
    """
    try:
        await bot.delete_messages(chat_id, message_id)
        logger.info(f"Deleted message {message_id} in chat {chat_id}")

    except Exception as e:
        logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
