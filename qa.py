# qa.py
"""
Q&A / MCQ game logic for the bot.
Tracks ongoing games, questions, and players' answers.
"""

from users import add_user, get_user

# In-memory storage for ongoing Q&A games
qa_games = {}  # {group_id: {"questions": [], "players_answers": {}, "finished": False}}

async def handle_qa_game(group_id: int, user_id: int, question_index: int, answer: str, username: str = None):
    """
    Handles a user's answer in a Q&A/MCQ game.

    Args:
        group_id (int): Telegram group ID
        user_id (int): Telegram user ID
        question_index (int): Index of the question being answered
        answer (str): User's answer
        username (str): Telegram username (optional)

    Returns:
        dict: Current game state for the group
    """
    # Ensure the user exists
    await add_user(user_id, username)

    # Initialize the game for the group if it doesn't exist
    if group_id not in qa_games:
        qa_games[group_id] = {
            "questions": [],           # List of dicts: {"q": str, "a": str}
            "players_answers": {},     # {(user_id, question_index): answer}
            "finished": False
        }

    game = qa_games[group_id]

    # Record the player's answer
    game["players_answers"][(user_id, question_index)] = answer

    return game


async def reset_qa_game(group_id: int):
    """
    Resets the Q&A game for a group.

    Args:
        group_id (int): Telegram group ID
    """
    qa_games[group_id] = {
        "questions": [],
        "players_answers": {},
        "finished": False
    }

    # Optional: remove the group entirely
    # qa_games.pop(group_id, None)
