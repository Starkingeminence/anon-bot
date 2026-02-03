import logging
from datetime import datetime
from users import get_user
from games import fastest_fingers, qa

logger = logging.getLogger(__name__)

# -----------------------------
# Generate combined leaderboard
# -----------------------------
async def generate_leaderboard(group_id: int):
    """
    Returns a combined leaderboard for all games in the group.
    Scores are summed across Fastest Fingers and Q&A/MCQ games.
    """
    scores = {}

    # -------------------------
    # Fastest Fingers results
    # -------------------------
    ff_game = fastest_fingers.fastest_fingers_games.get(group_id)
    if ff_game:
        winners = ff_game.get("winners", [])
        for idx, user_id in enumerate(winners):
            # Assign points decreasing: first=3, second=2, third=1
            points = ff_game["max_winners"] - idx
            scores[user_id] = scores.get(user_id, 0) + points

    # -------------------------
    # Q&A/MCQ results
    # -------------------------
    qa_game = qa.qa_games.get(group_id)
    if qa_game and qa_game.get("finished", False):
        # Sum scores from players_answers
        for key, answer in qa_game["players_answers"].items():
            user_id, q_index = key
            correct_answer = qa_game["questions"][q_index]["a"].lower()
            if answer.lower() == correct_answer:
                scores[user_id] = scores.get(user_id, 0) + 1

    # -------------------------
    # Build leaderboard
    # -------------------------
    if not scores:
        return "No scores yet."

    leaderboard = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    leaderboard_text = "🏆 Leaderboard\n"
    for rank, (user_id, pts) in enumerate(leaderboard, start=1):
        user = await get_user(user_id)
        username = user["username"] if user and user["username"] else str(user_id)
        leaderboard_text += f"{rank}. @{username} — {pts} pts\n"

    return leaderboard_text
