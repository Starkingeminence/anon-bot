# groups.py

from connection import db


async def handle_new_group(chat_id: int):
    """
    Ensures a group exists in the database.
    Called on every message but exits quickly if already registered.
    """
    if not chat_id:
        return

    query = """
    INSERT INTO groups (group_id)
    VALUES ($1)
    ON CONFLICT (group_id) DO NOTHING
    """

    try:
        await db.execute(query, chat_id)
    except Exception:
        # Fail silently to avoid crashing the bot
        pass
