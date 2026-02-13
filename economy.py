# economy.py
import logging
from datetime import datetime, timedelta
from connection import db

logger = logging.getLogger(__name__)

# ==========================
# TIERS CONFIG
# ==========================
TIERS = {
    "free": {"max_members": 1000, "governance": False},
    "pro": {"max_members": 50000, "governance": True},
    "pro+": {"max_members": 500000, "governance": True},
    "enterprise": {"max_members": None, "governance": True}
}

async def set_group_tier(group_id: int, tier_name: str):
    # Assumes a 'groups' table exists with a tier column
    await db.execute("UPDATE groups SET tier = $1 WHERE group_id = $2", tier_name, group_id)

async def get_tier_config(group_id: int) -> dict:
    row = await db.fetchrow("SELECT tier FROM groups WHERE group_id = $1", group_id)
    tier_name = row["tier"] if row else "free"
    return TIERS.get(tier_name, TIERS["free"])

# ==========================
# PAYMENTS & GRACE
# ==========================
GRACE_PERIOD_DAYS = 7

async def verify_payment(memo: str, amount: float, group_id: int, expected_amount: float):
    # Logic to check blockchain API would go here
    if amount >= expected_amount and memo == str(group_id):
        return True
    return False

async def activate_subscription(group_id: int, tier: str, days: int = 30):
    start = datetime.utcnow()
    end = start + timedelta(days=days)
    await db.execute("""
        INSERT INTO subscriptions (group_id, tier, start_date, end_date, status)
        VALUES ($1, $2, $3, $4, 'active')
        ON CONFLICT (group_id) DO UPDATE 
        SET tier=EXCLUDED.tier, end_date=EXCLUDED.end_date, status='active'
    """, group_id, tier, start, end)
    await set_group_tier(group_id, tier)

async def check_subscriptions_and_grace(bot):
    """Daily check for expired subs."""
    subs = await db.fetch("SELECT * FROM subscriptions")
    now = datetime.utcnow()
    
    for sub in subs:
        if sub["status"] == "active" and now > sub["end_date"]:
            # Move to grace
            new_end = now + timedelta(days=GRACE_PERIOD_DAYS)
            await db.execute("UPDATE subscriptions SET status='grace', end_date=$1 WHERE group_id=$2", new_end, sub["group_id"])
            
        elif sub["status"] == "grace" and now > sub["end_date"]:
            # Expire
            await db.execute("UPDATE subscriptions SET status='expired' WHERE group_id=$1", sub["group_id"])
            await set_group_tier(sub["group_id"], "free")
            # Notify owner logic here...

