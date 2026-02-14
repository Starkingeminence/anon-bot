"""
Games Module for Eminence DAO Bot
Merged: fastest_fingers.py, qa.py, leaderboard.py

Responsibilities:
- Q&A / MCQ games
- Fastest finger challenges
- Leaderboards and point tracking
"""

from users import add_user, get_user
import asyncio

# -----------------------------
# Leaderboard storage
# -----------------------------
leaderboards = {}  # {group_id: {user_id: points}}

# -----------------------------
# Q&A / MCQ Game storage
# -----------------------------
qa_games = {}  # {group_id: {"questions": [], "players_answers": {}, "finished": False}}

async def handle_qa_game(group_id: int, user_id: int, question_index: int, answer: str, username: str = None):
    """
    Handles a user's answer in a Q&A/MCQ game.
    """
    # Ensure the user exists
    await add_user(user_id, username)

    # Initialize the game for the group if it doesn't exist
    if group_id not in qa_games:
        qa_games[group_id] = {
            "questions": [],
            "players_answers": {},
            "finished": False
        }

    game = qa_games[group_id]

    # Record the player's answer
    game["players_answers"][(user_id, question_index)] = answer

    return game


async def reset_qa_game(group_id: int):
    """
    Resets the Q&A game for a group.
    """
    qa_games[group_id] = {
        "questions": [],
        "players_answers": {},
        "finished": False
    }

# -----------------------------
# Fastest Finger Game storage
# -----------------------------
fastest_games = {}  # {group_id: {"question": str, "answer": str, "winner": user_id or None}}

async def start_fastest_game(group_id: int, question: str, answer: str):
    """
    Starts a fastest finger game for a group.
    """
    fastest_games[group_id] = {
        "question": question,
        "answer": answer.lower(),
        "winner": None
    }

async def submit_fastest_answer(group_id: int, user_id: int, user_answer: str, username: str = None):
    """
    Submits an answer for the fastest finger game.
    First correct answer is winner.
    """
    await add_user(user_id, username)
    game = fastest_games.get(group_id)
    if not game or game["winner"] is not None:
        return False  # Game not active or already won

    if user_answer.lower() == game["answer"]:
        game["winner"] = user_id
        # Award points on leaderboard
        leaderboards.setdefault(group_id, {})
        leaderboards[group_id][user_id] = leaderboards[group_id].get(user_id, 0) + 3  # Fastest correct points
        return True

    return False

async def end_fastest_game(group_id: int):
    """
    Ends the fastest finger game for a group.
    """
    fastest_games.pop(group_id, None)

# -----------------------------
# Leaderboard functions
# -----------------------------

def get_leaderboard(group_id: int, top_n: int = 10):
    """
    Returns top N users by points in a group.
    """
    group_board = leaderboards.get(group_id, {})
    # Sort by points descending
    sorted_board = sorted(group_board.items(), key=lambda x: x[1], reverse=True)
    return sorted_board[:top_n]

async def award_points(group_id: int, user_id: int, points: int, username: str = None):
    """
    Award points to a user on the leaderboard.
    """
    await add_user(user_id, username)
    leaderboards.setdefault(group_id, {})
    leaderboards[group_id][user_id] = leaderboards[group_id].get(user_id, 0) + points

async def reset_leaderboard(group_id: int):
    """
    Resets the leaderboard for a group.
    """
    leaderboards[group_id] = {}

def register_moderation_handlers(app):
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(MessageHandler(filters.ALL, moderation_guard))
