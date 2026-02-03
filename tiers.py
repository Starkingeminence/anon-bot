import logging
from connection import db
from groups import set_group_tier, get_group_tier

logger = logging.getLogger(__name__)

# -----------------------------
# Tier configuration
# -----------------------------
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
        "max_members": None,  # No hard limit
        "static_tone": False,
        "basic_logging": True,
        "limited_anon_messages": False,
        "governance": True,
        "custom": True
    }
}

# -----------------------------
# Tier management
# -----------------------------
async def set_tier_for_group(group_id: int, tier_name: str):
    """
    Set the subscription tier for a group.
    """
    if tier_name not in TIERS:
        raise ValueError(f"Invalid tier: {tier_name}")

    await set_group_tier(group_id, tier_name)
    logger.info(f"Set tier '{tier_name}' for group {group_id}")


async def get_tier_for_group(group_id: int) -> dict:
    """
    Return the tier configuration dictionary for a group.
    """
    tier_name = await get_group_tier(group_id)
    return TIERS.get(tier_name, TIERS["free"])


# -----------------------------
# Check if group exceeds tier limits
# -----------------------------
async def check_group_member_limit(group_id: int, current_member_count: int) -> bool:
    """
    Returns True if the group exceeds its tier member limit.
    """
    tier = await get_tier_for_group(group_id)
    max_members = tier.get("max_members")
    if max_members is None:
        return False  # Unlimited members
    return current_member_count > max_members
