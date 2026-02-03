# captcha.py

from connection import db


async def verify_captcha(user_id: int, chat_id: int, message: str):
    """
    Placeholder captcha verification.
    Currently allows all messages.
    """
    # Future logic goes here (timeouts, retries, kicks, etc.)
    return True
