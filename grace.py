import asyncio
from datetime import datetime, timedelta
from connection import db
from groups import set_group_tier
import logging

logger = logging.getLogger(__name__)

# -----------------------------
# Grace period configuration
# -----------------------------
FIRST_TIME_GRACE_DAYS = 14
SUBSEQUENT_GRACE_DAYS = 7
MIN_GRACE_DAYS = 1  # don't go below this
ABUSE_THRESHOLDS = [7, 3, 1]  # decrease grace if repeatedly paid late

# -----------------------------
# Check subscriptions daily
# -----------------------------
async def check_subscriptions(bot):
    """
    Check all subscriptions and handle grace periods / expiration.
    Should be called periodically (e.g., cron job or background task).
    """
    query = "SELECT * FROM subscriptions"
    subscriptions = await db.fetch(query)

    for sub in subscriptions:
        group_id = sub["group_id"]
        status = sub["status"]
        end_date = sub["end_date"]
        tier = sub["tier"]

        now = datetime.utcnow()
        days_left = (end_date - now).days if end_date else 0

        # Subscription active but nearing end
        if status == "active" and days_left <= SUBSEQUENT_GRACE_DAYS:
            await notify_owner_grace(bot, group_id, days_left)

        # Subscription expired
        if status == "active" and now > end_date:
            await start_grace_period(group_id)

        # Grace period expired
        if status == "grace" and now > end_date:
            await expire_subscription(group_id)


# -----------------------------
# Notify owner about grace period
# -----------------------------
async def notify_owner_grace(bot, group_id: int, days_left: int):
    """
    Notify the owner that the subscription is nearing expiration.
    """
    # Get owner
    query = "SELECT user_id FROM permissions WHERE group_id = $1 AND role = 'owner'"
    owner_row = await db.fetchrow(query, group_id)
    if not owner_row:
        return

    owner_id = owner_row["user_id"]
    try:
        await bot.send_message(
            owner_id,
            f"⚠️ Your group's subscription (ID: {group_id}) will expire in {days_left} days. "
            f"Please renew to continue using the bot."
        )
        logger.info(f"Notified owner {owner_id} of grace for group {group_id}")
    except Exception as e:
        logger.error(f"Failed to notify owner {owner_id} about grace: {e}")


# -----------------------------
# Start grace period
# -----------------------------
async def start_grace_period(group_id: int):
    """
    Switch subscription to grace period for late payment.
    """
    now = datetime.utcnow()
    # Default grace period
    grace_days = SUBSEQUENT_GRACE_DAYS

    # Update subscription table
    query = """
        UPDATE subscriptions
        SET status = 'grace',
            end_date = $2 + interval '$3 days'
        WHERE group_id = $1
    """
    new_end = now
    await db.execute(query, group_id, new_end, grace_days)
    logger.info(f"Started grace period ({grace_days} days) for group {group_id}")


# -----------------------------
# Expire subscription
# -----------------------------
async def expire_subscription(group_id: int):
    """
    Disconnect the bot from a group whose subscription and grace period have expired.
    """
    # Update subscription status
    query = "UPDATE subscriptions SET status = 'expired' WHERE group_id = $1"
    await db.execute(query, group_id)

    # Optionally, disable bot features for this group
    await set_group_tier(group_id, "free")  # downgrade to free tier
    logger.info(f"Subscription for group {group_id} expired. Features disabled.")

    # Notify owner/admin
    owner_query = "SELECT user_id FROM permissions WHERE group_id = $1 AND role = 'owner'"
    owner_row = await db.fetchrow(owner_query, group_id)
    if owner_row:
        owner_id = owner_row["user_id"]
        try:
            from client import bot  # Ensure bot is imported
            await bot.send_message(
                owner_id,
                f"⚠️ Your group's subscription (ID: {group_id}) has expired and the bot has been disabled."
            )
        except Exception as e:
            logger.error(f"Failed to notify owner {owner_id} about expired subscription: {e}")
