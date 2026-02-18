# groups.py

from connection import db
import logging

logger = logging.getLogger(__name__)

# -----------------------------
# CREATE / REGISTER GROUP
# -----------------------------
async def handle_new_group(chat_id: int, name: str = None, owner_id: int = None):
    """
    Ensures a group exists in the database.
    Called on every message but exits quickly if already registered.
    """
    if not chat_id:
        return

    query = """
    INSERT INTO groups (chat_id, name, owner_id)
    VALUES ($1, $2, $3)
    ON CONFLICT (chat_id) DO NOTHING
    """

    try:
        await db.execute(query, chat_id, name, owner_id)
    except Exception as e:
        logger.error(f"Failed to register group {chat_id}: {e}")

# -----------------------------
# FETCH GROUP INFO
# -----------------------------
async def get_group_info(chat_id: int):
    """
    Returns a dictionary with group info: id, chat_id, name, owner_id, tier, start_date, created_at
    """
    query = "SELECT * FROM groups WHERE chat_id = $1"
    row = await db.fetchrow(query, chat_id)
    if not row:
        return None

    return dict(row)

# -----------------------------
# GET / SET GROUP TIER
# -----------------------------
async def get_group_tier(chat_id: int):
    """
    Returns the tier of the group (string), defaults to 'free' if not set.
    """
    query = "SELECT tier FROM groups WHERE chat_id = $1"
    row = await db.fetchrow(query, chat_id)
    if not row or not row.get("tier"):
        return "free"
    return row["tier"]

async def set_group_tier(chat_id: int, tier: str):
    """
    Sets the tier of a group.
    """
    query = """
    UPDATE groups
    SET tier = $2
    WHERE chat_id = $1
    """
    try:
        await db.execute(query, chat_id, tier)
        logger.info(f"Set tier '{tier}' for group {chat_id}")
    except Exception as e:
        logger.error(f"Failed to set tier for group {chat_id}: {e}")
