import logging
from datetime import datetime, timedelta
from database.connection import db  # async DB connection

logger = logging.getLogger(__name__)

# -----------------------------
# Votes storage example
# -----------------------------
# votes_cast: {decision_id: {admin_id: vote_value}}
votes_cast = {}

# -----------------------------
# Create a new vote
# -----------------------------
async def create_vote(decision_id: int, admin_ids: list[int]):
    """
    Initialize a voting session for a decision with the admins who can vote.
    """
    votes_cast[decision_id] = {admin_id: None for admin_id in admin_ids}
    logger.info(f"Vote created for decision {decision_id} with admins {admin_ids}")

# -----------------------------
# Cast a vote
# -----------------------------
async def handle_vote(decision_id: int, admin_id: int, vote_value: str):
    """
    Records a vote for a given decision by an admin.
    """
    if decision_id not in votes_cast:
        logger.warning(f"Decision {decision_id} not found for voting")
        return False

    if admin_id not in votes_cast[decision_id]:
        logger.warning(f"Admin {admin_id} is not allowed to vote on decision {decision_id}")
        return False

    votes_cast[decision_id][admin_id] = vote_value
    logger.info(f"Admin {admin_id} voted '{vote_value}' on decision {decision_id}")
    return True

# -----------------------------
# Check if an admin has voted
# -----------------------------
def has_voted(decision_id: int, admin_id: int) -> bool:
    """
    Returns True if the admin has already voted on the decision.
    """
    return decision_id in votes_cast and admin_id in votes_cast[decision_id] and votes_cast[decision_id][admin_id] is not None

# -----------------------------
# Count votes for a decision
# -----------------------------
def count_votes(decision_id: int) -> dict:
    """
    Returns a dictionary summarizing the votes for a decision.
    Example output: {"yes": 2, "no": 1}
    """
    result = {}
    if decision_id not in votes_cast:
        return result

    for vote in votes_cast[decision_id].values():
        if vote is not None:
            result[vote] = result.get(vote, 0) + 1
    return result

# -----------------------------
# Expire old votes from the database (optional)
# -----------------------------
async def expire_old_votes(expire_minutes: int = 60):
    """
    Remove votes from the database that are older than expire_minutes.
    """
    now = datetime.utcnow()
    query = "SELECT decision_id FROM votes WHERE created_at < $1"
    expired_votes = await db.fetch(query, now - timedelta(minutes=expire_minutes))
    for row in expired_votes:
        decision_id = row["decision_id"]
        votes_cast.pop(decision_id, None)
        delete_query = "DELETE FROM votes WHERE decision_id = $1"
        await db.execute(delete_query, decision_id)
        logger.info(f"Expired votes for decision {decision_id}")
