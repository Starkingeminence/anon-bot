# voting.py
import asyncio

# Stores votes per decision
# Structure: { decision_id: { admin_id: vote_value } }
votes_cast = {}

# -----------------------------
# Cast a vote
# -----------------------------
async def cast_vote(admin_id: int, decision_id: str, vote: str):
    """
    Registers a vote from an admin on a given decision.
    Each admin can vote only once per decision.
    """
    if decision_id not in votes_cast:
        votes_cast[decision_id] = {}

    # Check if admin already voted
    if admin_id in votes_cast[decision_id]:
        return "You already voted on this decision!"

    # Register the vote
    votes_cast[decision_id][admin_id] = vote
    return "Vote accepted ✅"

# -----------------------------
# Tally votes
# -----------------------------
async def tally_votes(decision_id: str):
    """
    Returns the results for a given decision.
    Output: { 'option1': 3, 'option2': 2 } etc.
    """
    if decision_id not in votes_cast:
        return {}

    results = {}
    for vote in votes_cast[decision_id].values():
        results[vote] = results.get(vote, 0) + 1
    return results

# -----------------------------
# Reset votes
# -----------------------------
async def reset_votes(decision_id: str):
    """
    Clears votes for a specific decision.
    """
    if decision_id in votes_cast:
        del votes_cast[decision_id]

# -----------------------------
# Helper: check if an admin has voted
# -----------------------------
async def has_voted(admin_id: int, decision_id: str):
    return decision_id in votes_cast and admin_id in votes_cast[decision_id]    expired_votes = await db.fetch(query, now)

    for vote in expired_votes:
        results = await end_vote(vote["id"])
        logger.info(f"Vote {vote['id']} in group {vote['group_id']} expired. Results: {results}")
