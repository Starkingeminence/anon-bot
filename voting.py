import logging
from database.connection import db
from core.permissions import get_on_duty_admins, is_admin
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# -----------------------------
# Create a new vote
# -----------------------------
async def create_vote(group_id: int, creator_id: int, topic: str, options: list, duration: int = 3600):
    """
    Create a new governance vote in a group.
    """
    query = """
        INSERT INTO votes (group_id, topic, options, created_by, duration, created_at, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        RETURNING id
    """
    vote_id = await db.fetchrow(query, group_id, topic, options, creator_id, duration, datetime.utcnow())
    logger.info(f"Created vote {vote_id['id']} for group {group_id} with topic '{topic}'")
    return vote_id['id']


# -----------------------------
# Cast a vote
# -----------------------------
async def cast_vote(vote_id: int, user_id: int, selected_option: str):
    """
    Cast a vote for a given user.
    """
    # Check if vote is active
    vote = await db.fetchrow("SELECT * FROM votes WHERE id = $1", vote_id)
    if not vote or not vote["is_active"]:
        return False

    # Optional: check if user is an admin or member if needed
    # Prevent duplicate votes
    query = """
        INSERT INTO vote_results (vote_id, user_id, selected_option, voted_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (vote_id, user_id)
        DO UPDATE SET selected_option = EXCLUDED.selected_option, voted_at = EXCLUDED.voted_at
    """
    await db.execute(query, vote_id, user_id, selected_option, datetime.utcnow())
    logger.info(f"User {user_id} voted '{selected_option}' in vote {vote_id}")
    return True


# -----------------------------
# Calculate results
# -----------------------------
async def calculate_vote_results(vote_id: int):
    """
    Returns a dictionary of option -> weighted votes.
    """
    vote = await db.fetchrow("SELECT * FROM votes WHERE id = $1", vote_id)
    if not vote:
        return None

    results = await db.fetch("SELECT * FROM vote_results WHERE vote_id = $1", vote_id)

    # Prepare tally
    tally = {option: 0 for option in vote["options"]}

    for row in results:
        user_id = row["user_id"]
        selected_option = row["selected_option"]
        weight = 1  # Default weight

        # Check if user is admin and on-duty (admins get full weight)
        if await is_admin(user_id, vote["group_id"]):
            on_duty = await get_on_duty_admins(vote["group_id"])
            if user_id in on_duty:
                weight = 1  # full weight for on-duty admin
            else:
                weight = 0.5  # off-duty admins count partially

        tally[selected_option] += weight

    return tally


# -----------------------------
# End a vote
# -----------------------------
async def end_vote(vote_id: int):
    """
    Marks a vote as inactive and returns the results.
    """
    results = await calculate_vote_results(vote_id)

    # Mark inactive
    await db.execute("UPDATE votes SET is_active = FALSE WHERE id = $1", vote_id)
    logger.info(f"Vote {vote_id} ended with results: {results}")
    return results


# -----------------------------
# Check for expired votes
# -----------------------------
async def check_expired_votes():
    """
    Automatically end votes whose duration has expired.
    """
    now = datetime.utcnow()
    query = """
        SELECT id, group_id, topic
        FROM votes
        WHERE is_active = TRUE AND created_at + (duration || ' seconds')::interval < $1
    """
    expired_votes = await db.fetch(query, now)

    for vote in expired_votes:
        results = await end_vote(vote["id"])
        logger.info(f"Vote {vote['id']} in group {vote['group_id']} expired. Results: {results}")
