import asyncio
import logging
from datetime import datetime, timedelta
from database.connection import db
from core.users import add_user, get_user
from telethon import TelegramClient, Button, events

logger = logging.getLogger(__name__)

# -----------------------------
# Game state
# -----------------------------
# {group_id: {game_id: {"question": str, "answer": str, "max_winners": int, "start_time": datetime, "players": {user_id: timestamp}, "finished": bool}}}
fastest_fingers_games = {}

# -----------------------------
# Start a new game
# -----------------------------
async def start_game(bot: TelegramClient, group_id: int, question: str, answer: str, max_winners: int = 3, duration_seconds: int = 15):
    """
    Starts a fastest fingers game in a group.
    """
    game_id = f"{group_id}_{datetime.utcnow().timestamp()}"
    fastest_fingers_games[group_id] = {
        "game_id": game_id,
        "question": question,
        "answer": answer.lower(),
        "max_winners": max_winners,
        "start_time": datetime.utcnow(),
        "duration": duration_seconds,
        "players": {},
        "finished": False,
        "winners": []
    }

    # Send question to group
    await bot.send_message(
        group_id,
        f"🏎️ Fastest Fingers Game Started!\nQuestion: {question}\nFirst {max_winners} correct answers win!\nYou have {duration_seconds} seconds!"
    )

    # Start timer for game
    asyncio.create_task(end_game_after_timeout(bot, group_id, duration_seconds))

    logger.info(f"Started fastest fingers game {game_id} in group {group_id}")


# -----------------------------
# Player submits answer
# -----------------------------
async def submit_answer(bot: TelegramClient, group_id: int, user_id: int, answer: str):
    """
    Process a player's answer submission.
    """
    if group_id not in fastest_fingers_games:
        return False, "No active game in this group."

    game = fastest_fingers_games[group_id]

    if game["finished"]:
        return False, "Game already finished."

    # Check if user already has a correct answer
    if user_id in game["winners"]:
        return False, "You already won this game."

    # Record timestamp for this attempt
    game["players"][user_id] = datetime.utcnow()

    # Check correctness
    if answer.lower() == game["answer"]:
        game["winners"].append(user_id)
        # Check if max winners reached
        if len(game["winners"]) >= game["max_winners"]:
            await end_game(bot, group_id)
        return True, "Correct!"
    else:
        return False, "Incorrect. Try again!"


# -----------------------------
# End game manually
# -----------------------------
async def end_game(bot: TelegramClient, group_id: int):
    """
    Ends the fastest fingers game and announces winners.
    """
    if group_id not in fastest_fingers_games:
        return

    game = fastest_fingers_games[group_id]
    if game["finished"]:
        return

    game["finished"] = True

    winners = game["winners"]
    if winners:
        winner_mentions = []
        for user_id in winners:
            user = await get_user(user_id)
            username = user["username"] if user else str(user_id)
            winner_mentions.append(f"@{username}")
        message = f"🏁 Fastest Fingers Game Ended!\nWinners: {', '.join(winner_mentions)}"
    else:
        message = f"🏁 Fastest Fingers Game Ended!\nNo winners this round."

    # Send result to group
    await bot.send_message(group_id, message)

    # Cleanup
    fastest_fingers_games.pop(group_id, None)
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
