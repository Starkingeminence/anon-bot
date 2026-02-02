import logging
from database.connection import db
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# -----------------------------
# Create a personal DM vote
# -----------------------------
async def create_personal_vote(creator_id: int, topic: str, options: list, duration_seconds: int = 3600):
    """
    Create a personal vote for a user via DM.
    """
    query = """
        INSERT INTO personal_votes (creator_id, topic, options, duration, created_at, is_active)
        VALUES ($1, $2, $3, $4, $5, TRUE)
        RETURNING id
    """
    row = await db.fetchrow(query, creator_id, topic, options, duration_seconds, datetime.utcnow())
    vote_id = row["id"] if row else None
    logger.info(f"Created personal vote {vote_id} by user {creator_id} on topic '{topic}'")
    return vote_id


# -----------------------------
# Cast a personal vote
# -----------------------------
async def cast_personal_vote(vote_id: int, voter_id: int, selected_option: str):
    """
    Cast a vote for a personal DM vote.
    """
    # Check if vote is active
    vote = await db.fetchrow("SELECT * FROM personal_votes WHERE id = $1", vote_id)
    if not vote or not vote["is_active"]:
        return False

    query = """
        INSERT INTO personal_vote_results (personal_vote_id, voter_id, selected_option, voted_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (personal_vote_id, voter_id)
        DO UPDATE SET selected_option = EXCLUDED.selected_option, voted_at = EXCLUDED.voted_at
    """
    await db.execute(query, vote_id, voter_id, selected_option, datetime.utcnow())
    logger.info(f"User {voter_id} voted '{selected_option}' in personal vote {vote_id}")
    return True


# -----------------------------
# Calculate results
# -----------------------------
async def get_personal_vote_results(vote_id: int):
    """
    Return the results for a personal vote as a dict of option -> count.
    """
    vote = await db.fetchrow("SELECT * FROM personal_votes WHERE id = $1", vote_id)
    if not vote:
        return None

    results = await db.fetch("SELECT selected_option FROM personal_vote_results WHERE personal_vote_id = $1", vote_id)
    tally = {option: 0 for option in vote["options"]}

    for row in results:
        option = row["selected_option"]
        if option in tally:
            tally[option] += 1
        else:
            tally[option] = 1  # in case of unexpected input

    return tally


# -----------------------------
# End personal vote
# -----------------------------
async def end_personal_vote(vote_id: int):
    """
    Mark a personal vote as inactive and return results.
    """
    results = await get_personal_vote_results(vote_id)
    await db.execute("UPDATE personal_votes SET is_active = FALSE WHERE id = $1", vote_id)
    logger.info(f"Personal vote {vote_id} ended. Results: {results}")
    return results


# -----------------------------
# Check expired personal votes
# -----------------------------
async def check_expired_personal_votes():
    """
    End votes that have passed their duration.
    """
    now = datetime.utcnow()
    query = """
        SELECT id, creator_id, topic
        FROM personal_votes
        WHERE is_active = TRUE AND created_at + (duration || ' seconds')::interval < $1
    """
    expired_votes = await db.fetch(query, now)

    for vote in expired_votes:
        results = await end_personal_vote(vote["id"])
        logger.info(f"Expired personal vote {vote['id']} by user {vote['creator_id']} ended. Results: {results}")
