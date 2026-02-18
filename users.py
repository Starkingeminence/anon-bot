from connection import db
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# In-memory cache
_users_cache = {}  # user_id -> dict

# -----------------------------
# Add / update user
# -----------------------------
async def add_user(user_id: int, username: str = None, full_name: str = None):
    now = datetime.utcnow()
    # Update cache first
    if user_id in _users_cache:
        user = _users_cache[user_id]
        if username:
            user["username"] = username
        if full_name:
            user["full_name"] = full_name
        user["last_seen"] = now
    else:
        user = {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "first_seen": now,
            "last_seen": now
        }
        _users_cache[user_id] = user

    # Persist in DB
    query = """
        INSERT INTO users (user_id, username, full_name, first_seen, last_seen)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            full_name = EXCLUDED.full_name,
            last_seen = EXCLUDED.last_seen
    """
    await db.execute(query, user_id, username, full_name, user["first_seen"], now)
    return user

# -----------------------------
# Get user
# -----------------------------
async def get_user(user_id: int):
    if user_id in _users_cache:
        return _users_cache[user_id]

    row = await db.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if not row:
        return None
    user = dict(row)
    _users_cache[user_id] = user
    return user

# -----------------------------
# List all users
# -----------------------------
async def list_users():
    rows = await db.fetch("SELECT * FROM users")
    users = [dict(r) for r in rows]
    for u in users:
        _users_cache[u["user_id"]] = u
    return users

# -----------------------------
# Convenience handler
# -----------------------------
async def handle_new_user(user_id: int, username: str = None, full_name: str = None):
    return await add_user(user_id, username, full_name)
