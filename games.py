"""
Games Module for Eminence DAO Bot
Merged: fastest_fingers.py, qa.py, leaderboard.py

Responsibilities:
- Q&A / MCQ games
- Fastest finger challenges
- Leaderboards and point tracking
- Privacy-safe: user IDs never exposed publicly
- Cached leaderboards for performance
"""

from users import add_user, get_user
from connection import db
import asyncio
from datetime import datetime, timedelta

# -----------------------------
# In-memory game storage
# -----------------------------
qa_games = {}  # {group_id: {"questions": [], "players_answers": {}, "finished": False}}
fastest_games = {}  # {group_id: {"question": str, "answer": str, "winner": user_id or None}}

# -----------------------------
# Leaderboard Cache
# -----------------------------
leaderboard_cache = {}  # {group_id: {"data": [...], "last_updated": datetime}}
CACHE_EXPIRY_SECONDS = 30  # refresh every 30 seconds

async def get_cached_leaderboard(group_id: int, top_n: int = 10):
    now = datetime.utcnow()
    cached = leaderboard_cache.get(group_id)
    if cached:
        age = (now - cached["last_updated"]).total_seconds()
        if age < CACHE_EXPIRY_SECONDS:
            return cached["data"]

    leaderboard = await get_leaderboard_db(group_id, top_n)
    leaderboard_cache[group_id] = {"data": leaderboard, "last_updated": now}
    return leaderboard

# -----------------------------
# Q&A / MCQ Game Functions
# -----------------------------
async def handle_qa_game(group_id: int, user_id: int, question_index: int, answer: str, username: str = None):
    await add_user(user_id, username)
    if group_id not in qa_games:
        qa_games[group_id] = {"questions": [], "players_answers": {}, "finished": False}
    game = qa_games[group_id]

    # Validate question index
    if question_index < 0 or question_index >= len(game["questions"]):
        raise IndexError("Question index out of range.")

    # Record answer
    game["players_answers"][(user_id, question_index)] = answer.strip()
    return game

async def reset_qa_game(group_id: int):
    qa_games[group_id] = {"questions": [], "players_answers": {}, "finished": False}

# -----------------------------
# Fastest Finger Game Functions
# -----------------------------
async def start_fastest_game(group_id: int, question: str, answer: str):
    fastest_games[group_id] = {"question": question, "answer": answer.strip().lower(), "winner": None}

async def submit_fastest_answer(group_id: int, user_id: int, user_answer: str, username: str = None):
    await add_user(user_id, username)
    game = fastest_games.get(group_id)
    if not game or game["winner"] is not None:
        return False
    if user_answer.strip().lower() == game["answer"]:
        game["winner"] = user_id
        await award_points_db(group_id, user_id, 3)  # award fastest correct points
        return True
    return False

async def end_fastest_game(group_id: int):
    fastest_games.pop(group_id, None)

# -----------------------------
# Leaderboard Functions (DB-backed, privacy-safe)
# -----------------------------
async def award_points_db(group_id: int, user_id: int, points: int):
    user = await get_user(user_id)
    display_name = user.get("username") or user.get("full_name") or f"Player {user_id}"
    query = """
        INSERT INTO leaderboard (group_id, user_id, username, points)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (group_id, user_id)
        DO UPDATE SET
            points = leaderboard.points + $4,
            username = EXCLUDED.username
    """
    await db.execute(query, group_id, user_id, display_name, points)

async def get_leaderboard_db(group_id: int, top_n: int = 10):
    query = """
        SELECT user_id, username, points
        FROM leaderboard
        WHERE group_id = $1
        ORDER BY points DESC
        LIMIT $2
    """
    rows = await db.fetch(query, group_id, top_n)
    leaderboard = []
    for row in rows:
        mention = f"[{row['username']}](tg://user?id={row['user_id']})"  # clickable name
        leaderboard.append((mention, row["points"]))
    return leaderboard

async def reset_leaderboard_db(group_id: int):
    query = "DELETE FROM leaderboard WHERE group_id = $1"
    await db.execute(query, group_id)
