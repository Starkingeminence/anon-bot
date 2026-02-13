import logging
from datetime import datetime, timedelta
from connection import db
from groups import set_group_tier, get_group_tier

logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURATION
# =========================================================

TON_WALLET_ADDRESS = "YOUR_BOT_WALLET_ADDRESS_HERE"

# Grace configuration
FIRST_TIME_GRACE_DAYS = 14
SUBSEQUENT_GRACE_DAYS = 7
MIN_GRACE_DAYS = 1

# =========================================================
# TIER CONFIGURATION
# =========================================================

TIERS = {
    "free": {
        "max_members": 1000,
        "static_tone": True,
        "basic_logging": True,
        "limited_anon_messages": True,
        "governance": False
    },
    "pro": {
        "max_members": 50000,
        "static_tone": False,
        "basic_logging": True,
        "limited_anon_messages": False,
        "governance": True
    },
    "pro+": {
        "max_members": 500000,
        "static_tone": False,
        "basic_logging": True,
        "limited_anon_messages": False,
        "governance": True
    },
    "enterprise": {
        "max_members": None,
        "static_tone": False,
        "basic_logging": True,
        "limited_anon_messages": False,
        "governance": True,
        "custom": True
    }
}

# =========================================================
# TIER MANAGEMENT
# =========================================================

async def set_tier_for_group(group_id: int, tier_name: str):
    if tier_name not in TIERS:
        raise ValueError(f"Invalid tier: {tier_name}")

    await set_group_tier(group_id, tier_name)
    logger.info(f"Set tier '{tier_name}' for group {group_id}")


async def get_tier_for_group(group_id: int) -> dict:
    tier_name = await get_group_tier(group_id)
    return TIERS.get(tier_name, TIERS["free"])


async def check_group_member_limit(group_id: int, current_member_count: int) -> bool:
    tier = await get_tier_for_group(group_id)
    max_members = tier.get("max_members")

    if max_members is None:
        return False

    return current_member_count > max_members


# =========================================================
# PAYMENT VERIFICATION (TON)
# =========================================================

async def verify_payment(
    memo: str,
    amount: float,
    group_id: int,
    expected_amount: float
) -> bool:
    """
    Verify TON payment.
    TODO: Integrate TON blockchain API.
    """

    logger.info(
        f"Verifying payment for group {group_id}, memo={memo}, amount={amount}"
    )

    # Placeholder logic
    if amount >= expected_amount and memo == str(group_id):
        return True

    return False


async def handle_payment_discrepancy(
    group_id: int,
    paid_amount: float,
    expected_amount: float
):
    if paid_amount < expected_amount:
        logger.warning(
            f"Group {group_id} underpaid. Paid: {paid_amount}, Expected: {expected_amount}"
        )
        # TODO: notify owner

    elif paid_amount > expected_amount:
        logger.info(
            f"Group {group_id} overpaid. Paid: {paid_amount}, Expected: {expected_amount}"
        )
        # TODO: apply extra credit


# =========================================================
# SUBSCRIPTION ACTIVATION
# =========================================================

async def activate_subscription(
    group_id: int,
    tier: str,
    duration_days: int = 30
):
    start_date = datetime.utcnow()
    end_date = start_date + timedelta(days=duration_days)

    query = """
        INSERT INTO subscriptions (group_id, tier, start_date, end_date, status)
        VALUES ($1, $2, $3, $4, 'active')
        ON CONFLICT (group_id)
        DO UPDATE SET
            tier = EXCLUDED.tier,
            start_date = EXCLUDED.start_date,
            end_date = EXCLUDED.end_date,
            status = 'active'
    """

    await db.execute(query, group_id, tier, start_date, end_date)
    await set_group_tier(group_id, tier)

    logger.info(
        f"Activated {tier} subscription for group {group_id} until {end_date}"
    )


# =========================================================
# SUBSCRIPTION LIFECYCLE
# =========================================================

async def check_subscriptions(bot):
    """
    Run daily via background scheduler.
    """

    query = "SELECT * FROM subscriptions"
    subscriptions = await db.fetch(query)

    now = datetime.utcnow()

    for sub in subscriptions:
        group_id = sub["group_id"]
        status = sub["status"]
        end_date = sub["end_date"]

        if not end_date:
            continue

        days_left = (end_date - now).days

        # Notify if nearing expiration
        if status == "active" and days_left <= SUBSEQUENT_GRACE_DAYS:
            await notify_owner_grace(bot, group_id, days_left)

        # Expired → start grace
        if status == "active" and now > end_date:
            await start_grace_period(group_id)

        # Grace expired → downgrade
        if status == "grace" and now > end_date:
            await expire_subscription(bot, group_id)


# =========================================================
# GRACE PERIOD HANDLING
# =========================================================

async def notify_owner_grace(bot, group_id: int, days_left: int):
    query = """
        SELECT user_id
        FROM permissions
        WHERE group_id = $1 AND role = 'owner'
    """

    owner_row = await db.fetchrow(query, group_id)
    if not owner_row:
        return

    owner_id = owner_row["user_id"]

    try:
        await bot.send_message(
            owner_id,
            f"⚠️ Your group's subscription will expire in {days_left} days."
        )
    except Exception as e:
        logger.error(f"Failed to notify owner {owner_id}: {e}")


async def start_grace_period(group_id: int):
    now = datetime.utcnow()
    grace_days = SUBSEQUENT_GRACE_DAYS

    query = """
        UPDATE subscriptions
        SET status = 'grace',
            end_date = $2
        WHERE group_id = $1
    """

    new_end = now + timedelta(days=grace_days)
    await db.execute(query, group_id, new_end)

    logger.info(
        f"Started grace period ({grace_days} days) for group {group_id}"
    )


async def expire_subscription(bot, group_id: int):
    query = """
        UPDATE subscriptions
        SET status = 'expired'
        WHERE group_id = $1
    """
    await db.execute(query, group_id)

    # Downgrade to free
    await set_group_tier(group_id, "free")

    logger.info(f"Subscription expired for group {group_id}")

    # Notify owner
    owner_query = """
        SELECT user_id
        FROM permissions
        WHERE group_id = $1 AND role = 'owner'
    """
    owner_row = await db.fetchrow(owner_query, group_id)

    if owner_row:
        owner_id = owner_row["user_id"]
        try:
            await bot.send_message(
                owner_id,
                "⚠️ Your subscription has expired. The bot has been downgraded to Free tier."
            )
        except Exception as e:
            logger.error(f"Failed to notify owner {owner_id}: {e}")
