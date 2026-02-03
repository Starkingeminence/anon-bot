import asyncio
import logging
from datetime import datetime, timedelta
from connection import db
from users import add_user, get_user
from telethon import TelegramClient, Button, events

logger = logging.getLogger(__name__)

# -----------------------------
# Game state
# -----------------------------
# {group_id: {game_id: {"type": "qa"|"mcq", "questions": [{"q": str, "a": str, "options": list}], "current_index": int, "players_answers": {user_id: str}, "start_time": datetime, "finished": bool}}}
qa_games = {}

# -----------------------------
# Start Q&A or MCQ game
# -----------------------------
async def start_qa_game(bot: TelegramClient, group_id: int, questions: list, game_type: str = "qa", duration_seconds: int = 15):
    """
    Start a Q&A or MCQ game.
    questions = [{"q": "Question text", "a": "Answer", "options": ["opt1","opt2","opt3"]}]
    """
    game_id = f"{group_id}_{datetime.utcnow().timestamp()}"
    qa_games[group_id] = {
        "game_id": game_id,
        "type": game_type,
        "questions": questions,
        "current_index": 0,
        "players_answers": {},
        "start_time": datetime.utcnow(),
        "duration": duration_seconds,
        "finished": False
    }

    # Send first question
    await send_current_question(bot, group_id)
    logger.info(f"Started {game_type} game {game_id} in group {group_id}")


# -----------------------------
# Send current question
# -----------------------------
async def send_current_question(bot: TelegramClient, group_id: int):
    """
    Sends the current question to the group with buttons if MCQ.
    """
    game = qa_games.get(group_id)
    if not game or game["finished"]:
        return

    current_index = game["current_index"]
    if current_index >= len(game["questions"]):
        await end_game(bot, group_id)
        return

    question_obj = game["questions"][current_index]
    q_text = question_obj["q"]
    options = question_obj.get("options", [])

    if game["type"] == "mcq" and options:
        buttons = [Button.inline(opt, data=opt) for opt in options]
        await bot.send_message(group_id, f"❓ Q{current_index+1}: {q_text}", buttons=buttons)
    else:
        await bot.send_message(group_id, f"❓ Q{current_index+1}: {q_text}\nReply with your answer within {game['duration']} seconds.")

    # Start timer for this question
    asyncio.create_task(question_timeout(bot, group_id, game["duration"]))


# -----------------------------
# Player submits answer
# -----------------------------
async def submit_answer(bot: TelegramClient, group_id: int, user_id: int, answer: str):
    """
    Process a player's answer for current question.
    Only first answer counts for QA and MCQ.
    """
    game = qa_games.get(group_id)
    if not game or game["finished"]:
        return False, "No active game."

    current_index = game["current_index"]

    # Single-answer rule per user per question
    key = (user_id, current_index)
    if key in game["players_answers"]:
        return False, "You have already answered this question."

    game["players_answers"][key] = answer.lower()
    return True, "Answer recorded."


# -----------------------------
# Timeout per question
# -----------------------------
async def question_timeout(bot: TelegramClient, group_id: int, duration: int):
    """
    Ends current question after duration.
    """
    await asyncio.sleep(duration)
    game = qa_games.get(group_id)
    if not game or game["finished"]:
        return

    # Move to next question
    game["current_index"] += 1
    await send_current_question(bot, group_id)


# -----------------------------
# End game
# -----------------------------
async def end_game(bot: TelegramClient, group_id: int):
    """
    End Q&A or MCQ game and show leaderboard.
    """
    game = qa_games.get(group_id)
    if not game or game["finished"]:
        return

    game["finished"] = True

    # Calculate scores
    scores = {}
    for key, answer in game["players_answers"].items():
        user_id, q_index = key
        correct_answer = game["questions"][q_index]["a"].lower()
        if answer == correct_answer:
            scores[user_id] = scores.get(user_id, 0) + 1

    # Format leaderboard
    if scores:
        leaderboard = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        leaderboard_text = "\n".join([f"{await get_username(uid)}: {pts} pts" for uid, pts in leaderboard])
        await bot.send_message(group_id, f"🏁 Game Ended! Leaderboard:\n{leaderboard_text}")
    else:
        await bot.send_message(group_id, "🏁 Game Ended! No one answered correctly.")

    qa_games.pop(group_id, None)
    logger.info(f"Q&A/MCQ game in group {group_id} ended.")


# -----------------------------
# Helper to get username
# -----------------------------
async def get_username(user_id: int):
    from core.users import get_user
    user = await get_user(user_id)
    return user["username"] if user and user["username"] else str(user_id)
