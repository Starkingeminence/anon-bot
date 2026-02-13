# governance.py
import logging
from datetime import datetime, timedelta
from connection import db

logger = logging.getLogger(__name__)

# ==========================
# ADMIN WEIGHTS
# ==========================

async def get_admin_weights(group_id: int):
    """Calculates voting weight: Owner 50%, Admins share remaining 50%."""
    # Fetch all admins
    all_admins_rows = await db.fetch("SELECT user_id FROM permissions WHERE group_id = $1 AND role = 'admin'", group_id)
    all_admins = [row["user_id"] for row in all_admins_rows]

    # Fetch owner
    owner_row = await db.fetchrow("SELECT user_id FROM permissions WHERE group_id = $1 AND role = 'owner'", group_id)
    owner_id = owner_row["user_id"] if owner_row else None

    weights = {}

    # Scenario: No owner, equal split
    if owner_id is None:
        if not all_admins: return weights
        equal_weight = 1 / len(all_admins)
        for admin in all_admins: weights[admin] = equal_weight
        return weights

    # Scenario: Owner exists
    weights[owner_id] = 1.0 if not all_admins else 0.5
    
    if all_admins:
        per_admin_weight = 0.5 / len(all_admins)
        for admin in all_admins:
            weights[admin] = per_admin_weight

    return weights

# ==========================
# ADMIN VOTING (In-Memory)
# ==========================

votes_cast = {} # {decision_id: {admin_id: vote_value}}

async def create_vote(decision_id: int, admin_ids: list):
    votes_cast[decision_id] = {admin_id: None for admin_id in admin_ids}
    logger.info(f"Vote created for decision {decision_id}")

async def handle_vote(decision_id: int, admin_id: int, vote_value: str):
    if decision_id not in votes_cast or admin_id not in votes_cast[decision_id]:
        return False
    votes_cast[decision_id][admin_id] = vote_value
    return True

def count_votes(decision_id: int) -> dict:
    if decision_id not in votes_cast: return {}
    result = {}
    for vote in votes_cast[decision_id].values():
        if vote: result[vote] = result.get(vote, 0) + 1
    return result

# ==========================
# PERSONAL VOTES (Database)
# ==========================

async def create_personal_vote(creator_id: int, topic: str, options: list, duration_seconds: int = 3600):
    query = """
        INSERT INTO personal_votes (creator_id, topic, options, duration, created_at, is_active)
        VALUES ($1, $2, $3, $4, $5, TRUE) RETURNING id
    """
    row = await db.fetchrow(query, creator_id, topic, options, duration_seconds, datetime.utcnow())
    return row["id"] if row else None

async def cast_personal_vote(vote_id: int, voter_id: int, selected_option: str):
    vote = await db.fetchrow("SELECT * FROM personal_votes WHERE id = $1", vote_id)
    if not vote or not vote["is_active"]: return False

    query = """
        INSERT INTO personal_vote_results (personal_vote_id, voter_id, selected_option, voted_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (personal_vote_id, voter_id)
        DO UPDATE SET selected_option = EXCLUDED.selected_option, voted_at = EXCLUDED.voted_at
    """
    await db.execute(query, vote_id, voter_id, selected_option, datetime.utcnow())
    return True

async def get_personal_vote_results(vote_id: int):
    vote = await db.fetchrow("SELECT options FROM personal_votes WHERE id = $1", vote_id)
    if not vote: return None
    results = await db.fetch("SELECT selected_option FROM personal_vote_results WHERE personal_vote_id = $1", vote_id)
    
    tally = {opt: 0 for opt in vote["options"]}
    for row in results:
        if row["selected_option"] in tally:
            tally[row["selected_option"]] += 1
    return tally

