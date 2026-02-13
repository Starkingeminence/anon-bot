"""
Governance Module
Eminence DAO Bot
Feb 2026

Responsibilities:
- Create decisions
- Cast votes with weight
- Editable until expiry
- Immutable record after expiry
- Audit-safe weighted results
"""

import asyncio
from datetime import datetime, timedelta
from db.connection import db  # assumes asyncpg wrapper

# -----------------------------
# DECISION MANAGEMENT
# -----------------------------
async def create_decision(title: str, description: str, expires_in_hours: int = 72):
    """
    Create a new governance decision.
    Status = active
    Expires_at = now + expires_in_hours
    """
    expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

    query = """
        INSERT INTO governance_decisions
        (title, description, status, created_at, expires_at)
        VALUES ($1, $2, 'active', NOW(), $3)
        RETURNING id
    """
    decision_id = await db.fetchval(query, title, description, expires_at)
    return decision_id

# -----------------------------
# VOTE CASTING
# -----------------------------
async def cast_vote(decision_id: int, voter_id: int, vote_value: str, weight: float):
    """
    Cast or update a vote.
    Editable only while decision is active and before expiry.
    """
    decision = await db.fetchrow(
        "SELECT status, expires_at FROM governance_decisions WHERE id = $1",
        decision_id
    )

    if not decision:
        return False, "Decision not found."

    if decision["status"] != "active":
        return False, "Decision is closed."

    if decision["expires_at"] and decision["expires_at"] < datetime.utcnow():
        return False, "Decision expired."

    query = """
        INSERT INTO governance_votes
        (decision_id, voter_id, vote_value, weight, voted_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (decision_id, voter_id)
        DO UPDATE SET
            vote_value = EXCLUDED.vote_value,
            weight = EXCLUDED.weight,
            voted_at = NOW()
    """

    await db.execute(query, decision_id, voter_id, vote_value, weight)
    return True, "Vote cast successfully."

# -----------------------------
# EXPIRE DECISIONS (LOCK)
# -----------------------------
async def expire_governance_decisions():
    """
    Mark active decisions past their expiry as closed.
    Returns list of expired decision IDs.
    """
    now = datetime.utcnow()
    query = """
        UPDATE governance_decisions
        SET status = 'closed'
        WHERE status = 'active'
        AND expires_at < $1
        RETURNING id
    """
    expired = await db.fetch(query, now)
    return [row["id"] for row in expired]

# -----------------------------
# GET RESULTS
# -----------------------------
async def get_decision_results(decision_id: int):
    """
    Returns weighted vote tally for a decision.
    Vote weights are immutable (stored at vote time)
    """
    query = """
        SELECT vote_value, SUM(weight) AS total_weight
        FROM governance_votes
        WHERE decision_id = $1
        GROUP BY vote_value
    """
    rows = await db.fetch(query, decision_id)
    return {row["vote_value"]: float(row["total_weight"]) for row in rows}

# -----------------------------
# CHECK VOTER STATUS
# -----------------------------
async def has_voted(decision_id: int, voter_id: int):
    """
    Returns True if the voter has cast a vote for the given decision
    """
    row = await db.fetchrow(
        "SELECT 1 FROM governance_votes WHERE decision_id=$1 AND voter_id=$2",
        decision_id, voter_id
    )
    return bool(row)

# -----------------------------
# AUTO-LOCKER BACKGROUND TASK
# -----------------------------
async def governance_expiry_scheduler(interval_sec: int = 60):
    """
    Background task to periodically expire decisions
    """
    while True:
        expired_ids = await expire_governance_decisions()
        if expired_ids:
            print(f"Expired decisions: {expired_ids}")
        await asyncio.sleep(interval_sec)

# -----------------------------
# UTILITIES
# -----------------------------
async def get_active_decisions():
    return await db.fetch("SELECT * FROM governance_decisions WHERE status='active'")

async def get_closed_decisions():
    return await db.fetch("SELECT * FROM governance_decisions WHERE status='closed'")

async def get_decision(decision_id: int):
    return await db.fetchrow("SELECT * FROM governance_decisions WHERE id=$1", decision_id)

async def get_votes(decision_id: int):
    return await db.fetch(
        "SELECT voter_id, vote_value, weight, voted_at FROM governance_votes WHERE decision_id=$1",
        decision_id
    )
