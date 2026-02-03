import logging
from datetime import datetime, timedelta
from connection import db
from groups import set_group_tier

logger = logging.getLogger(__name__)

# -----------------------------
# Payment configuration
# -----------------------------
GRACE_PERIOD_FIRST_TIME = 14  # days
GRACE_PERIOD_SUBSEQUENT = 7   # days
GRACE_DECREASE_ABUSE = [7, 3, 1]  # days if repeatedly late
TON_WALLET_ADDRESS = "YOUR_BOT_WALLET_ADDRESS_HERE"

# -----------------------------
# Verify payment
# -----------------------------
async def verify_payment(memo: str, amount: float, group_id: int, expected_amount: float):
    """
    Verify that a payment was made to the correct memo for a group.
    """
    # TODO: Integrate with TON blockchain API / node
    # This function should:
    # - Fetch transactions to TON_WALLET_ADDRESS
    # - Filter by memo
    # - Match amount
    # Return True if valid, False otherwise
    # For now, assume verification always succeeds
    logger.info(f"Verifying payment for group {group_id}, memo: {memo}, amount: {amount}")
    if amount >= expected_amount and memo == str(group_id):
        return True
    return False

# -----------------------------
# Activate subscription
# -----------------------------
async def activate_subscription(group_id: int, tier: str, duration_days: int = 30):
    """
    Activate subscription for a group after payment.
    """
    start_date = datetime.utcnow()
    end_date = start_date + timedelta(days=duration_days)

    # Update group settings and subscription table
    query = """
        INSERT INTO subscriptions (group_id, tier, start_date, end_date, status)
        VALUES ($1, $2, $3, $4, 'active')
        ON CONFLICT (group_id)
        DO UPDATE SET tier = EXCLUDED.tier, start_date = EXCLUDED.start_date,
                      end_date = EXCLUDED.end_date, status = 'active'
    """
    await db.execute(query, group_id, tier, start_date, end_date)

    # Update tier in group settings
    await set_group_tier(group_id, tier)

    logger.info(f"Activated {tier} subscription for group {group_id} until {end_date}")


# -----------------------------
# Handle overpayment / underpayment
# -----------------------------
async def handle_payment_discrepancy(group_id: int, paid_amount: float, expected_amount: float):
    """
    Handle cases where payment is over or under the expected amount.
    """
    if paid_amount < expected_amount:
        # Notify admin / owner to pay the remaining balance
        logger.warning(f"Group {group_id} underpaid. Paid: {paid_amount}, Expected: {expected_amount}")
        # Implementation: send DM to owner/admin
    elif paid_amount > expected_amount:
        # Overpayment: apply to next month or current tier
        logger.info(f"Group {group_id} overpaid. Paid: {paid_amount}, Expected: {expected_amount}")
        # Implementation: credit extra month or apply to current tier
