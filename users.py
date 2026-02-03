# users.py

from connection import db


async def handle_new_user(user_id: int, chat_id: int):
    """
    Registers a user if they do not already exist.
    """
    if not user_id:
        return

    query = """
    INSERT INTO users (user_id)
    VALUES ($1)
    ON CONFLICT (user_id) DO NOTHING
    """

    try:
        await db.execute(query, user_id)
    except Exception:
        pass
