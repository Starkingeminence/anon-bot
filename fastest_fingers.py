# fastest_fingers.py
"""
Fastest Fingers game logic for the bot.
Tracks ongoing games, winners, and handles new game rounds.
"""

from users import add_user, get_user

# In-memory storage for ongoing Fastest Fingers games
fastest_fingers_games = {}  # {group_id: {"winners": [], "max_winners": 3}}

async def handle_fastest_fingers(group_id: int, user_id: int, username: str = None):
    """
    Handles a new participant in a Fastest Fingers game.
    Adds user to the game and returns updated winners list.

    Args:
        group_id (int): Telegram group ID
        user_id (int): Telegram user ID
        username (str): Telegram username

    Returns:
        dict: Current game state for the group
    """
    # Ensure the user exists
    await add_user(user_id, username)

    # Initialize game for the group if it doesn't exist
    if group_id not in fastest_fingers_games:
        fastest_fingers_games[group_id] = {
            "winners": [],
            "max_winners": 3
        }

    game = fastest_fingers_games[group_id]

    # Add user to winners list if not already present and limit to max_winners
    if user_id not in game["winners"] and len(game["winners"]) < game["max_winners"]:
        game["winners"].append(user_id)

    return game


async def reset_game(group_id: int):
    """
    Resets the Fastest Fingers game for a group.
    
    Args:
        group_id (int): Telegram group ID
    """
    if group_id in fastest_fingers_games:
        fastest_fingers_games[group_id] = {
            "winners": [],
            "max_winners": 3
        }    fastest_fingers_games.pop(group_id, None)
    logger.info(f"Game in group {group_id} ended. Winners: {winners}")


# -----------------------------
# Timer-based automatic ending
# -----------------------------
async def end_game_after_timeout(bot: TelegramClient, group_id: int, duration_seconds: int):
    """
    Ends the game after duration expires.
    """
    await asyncio.sleep(duration_seconds)
    if group_id in fastest_fingers_games:
        await end_game(bot, group_id)
