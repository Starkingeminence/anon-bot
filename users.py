# users.py
"""
In-memory user management for the bot.
Provides functions to add and retrieve users.
"""

# In-memory "database" of users
_users_db = {}

async def add_user(user_id: int, username: str = None):
    """
    Add a new user or update an existing user.
    
    Args:
        user_id (int): Telegram user ID
        username (str): Telegram username
    
    Returns:
        dict: User record
    """
    if user_id in _users_db:
        # Update username if provided
        if username:
            _users_db[user_id]["username"] = username
    else:
        # Create new user
        _users_db[user_id] = {
            "user_id": user_id,
            "username": username
        }
    return _users_db[user_id]


async def get_user(user_id: int):
    """
    Retrieve a user by user_id.
    
    Args:
        user_id (int): Telegram user ID
    
    Returns:
        dict or None: User record if exists, else None
    """
    return _users_db.get(user_id)


async def list_users():
    """
    Return a list of all users.
    
    Returns:
        list[dict]: All user records
    """
    return list(_users_db.values())
