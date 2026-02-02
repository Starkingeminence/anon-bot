import logging
from telethon import TelegramClient, Button
from typing import List, Optional

logger = logging.getLogger(__name__)

# -----------------------------
# Send DM to a user
# -----------------------------
async def send_dm(bot: TelegramClient, user_id: int, message: str, buttons: Optional[List[Button]] = None):
    """
    Sends a private message to a user.
    """
    try:
        if buttons:
            await bot.send_message(user_id, message, buttons=buttons)
        else:
            await bot.send_message(user_id, message)
        logger.info(f"Sent DM to {user_id}")
    except Exception as e:
        logger.error(f"Failed to send DM to {user_id}: {e}")


# -----------------------------
# Send message to a group
# -----------------------------
async def send_group_message(bot: TelegramClient, group_id: int, message: str, buttons: Optional[List[Button]] = None):
    """
    Sends a message to a group or channel.
    """
    try:
        if buttons:
            await bot.send_message(group_id, message, buttons=buttons)
        else:
            await bot.send_message(group_id, message)
        logger.info(f"Sent message to group {group_id}")
    except Exception as e:
        logger.error(f"Failed to send message to group {group_id}: {e}")


# -----------------------------
# Send inline buttons
# -----------------------------
async def send_buttons(bot: TelegramClient, chat_id: int, message: str, button_list: List[List[Button]]):
    """
    Sends a message with inline buttons arranged in rows.
    button_list: list of list of buttons (rows)
    """
    try:
        await bot.send_message(chat_id, message, buttons=button_list)
        logger.info(f"Sent buttons to {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send buttons to {chat_id}: {e}")


# -----------------------------
# Edit a message
# -----------------------------
async def edit_message(bot: TelegramClient, chat_id: int, message_id: int, new_text: str, buttons: Optional[List[Button]] = None):
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


# -----------------------------
# Delete a message
# -----------------------------
async def delete_message(bot: TelegramClient, chat_id: int, message_id: int):
    """
    Delete a message from chat.
    """
    try:
        await bot.delete_messages(chat_id, message_id)
        logger.info(f"Deleted message {message_id} in chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
