# games.py
import logging
from users import add_user, get_user  # Assumes users.py exists

logger = logging.getLogger(__name__)

# ==========================
# STATE STORAGE
# ==========================
# Stores game state: {group_id: {...}}
fastest_fingers_games = {} 
qa_games = {}

# ==========================
# FASTEST FINGERS LOGIC
# ==========================

async def handle_fastest_fingers(group_id: int, user_id: int, username: str = None):
    """
    Handles a user participating in Fastest Fingers.
    Returns the updated game state.
    """
    await add_user(user_id, username)

    # Initialize if missing
    if group_id not in fastest_fingers_games:
        fastest_fingers_games[group_id] = {
            "winners": [],
            "max_winners": 3
        }

    game = fastest_fingers_games[group_id]

    # Add to winners if spots represent
    if user_id not in game["winners"] and len(game["winners"]) < game["max_winners"]:
        game["winners"].append(user_id)

    return game

async def reset_fastest_fingers(group_id: int):
    fastest_fingers_games[group_id] = {"winners": [], "max_winners": 3}

# ==========================
# Q&A / MCQ LOGIC
# ==========================

async def handle_qa_game(group_id: int, user_id: int, q_index: int, answer: str, username: str = None):
    """
    Records a user's answer for a Q&A game.
    """
    await add_user(user_id, username)

    if group_id not in qa_games:
        qa_games[group_id] = {
            "questions": [],       # [{"q": "text", "a": "answer"}]
            "players_answers": {}, # {(user_id, q_index): "answer"}
            "finished": False
        }

    game = qa_games[group_id]
    game["players_answers"][(user_id, q_index)] = answer
    return game

async def reset_qa_game(group_id: int):
    qa_games[group_id] = {
        "questions": [],
        "players_answers": {},
        "finished": False
    }

# ==========================
# LEADERBOARD LOGIC
# ==========================

async def generate_leaderboard(group_id: int):
    """
    Calculates combined scores from Fastest Fingers and Q&A.
    """
    scores = {}

    # 1. Tally Fastest Fingers
    ff_game = fastest_fingers_games.get(group_id)
    if ff_game:
        for idx, user_id in enumerate(ff_game["winners"]):
            # Points: 1st=3, 2nd=2, 3rd=1
            points = max(3 - idx, 1)
            scores[user_id] = scores.get(user_id, 0) + points

    # 2. Tally Q&A
    qa_game = qa_games.get(group_id)
    if qa_game and qa_game.get("finished"):
        for (user_id, q_idx), ans in qa_game["players_answers"].items():
            # simple exact match check
            correct_ans = qa_game["questions"][q_idx]["a"].lower()
            if ans.lower() == correct_correct:
                scores[user_id] = scores.get(user_id, 0) + 1

    if not scores:
        return "🏆 Leaderboard: No scores yet."

    # 3. Format Output
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    text = "🏆 **Leaderboard**\n"
    
    for rank, (uid, pts) in enumerate(sorted_scores, 1):
        user = await get_user(uid)
        name = user["username"] if user and user["username"] else str(uid)
        text += f"{rank}. @{name} — {pts} pts\n"

    return text
      
