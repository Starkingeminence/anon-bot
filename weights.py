from permissions import get_on_duty_admins, is_admin, is_owner
from datetime import datetime

# -----------------------------
# Calculate admin weights
# -----------------------------

async def get_admin_weights(group_id: int):
    """
    Returns a dictionary of user_id -> weight for all admins in a group.
    Handles:
    - Owner leaves: remaining admins split weight
    - Only owner remains: owner gets 100%
    - Off-duty admins: may have reduced weight (optional)
    """
    # Fetch all admins in the group
    on_duty_admins = await get_on_duty_admins(group_id)

    # Fetch all admins including off-duty
    # Assuming a helper function exists to get all admins
    all_admins_query = """
        SELECT user_id FROM permissions
        WHERE group_id = $1 AND role = 'admin'
    """
    from database.connection import db
    all_admins_rows = await db.fetch(all_admins_query, group_id)
    all_admins = [row["user_id"] for row in all_admins_rows]

    # Fetch owner
    owners_query = """
        SELECT user_id FROM permissions
        WHERE group_id = $1 AND role = 'owner'
    """
    owner_row = await db.fetchrow(owners_query, group_id)
    owner_id = owner_row["user_id"] if owner_row else None

    weights = {}

    if owner_id is None:
        # No owner: all admins share equally
        num_admins = len(all_admins)
        if num_admins == 0:
            return weights  # no admins
        equal_weight = 1 / num_admins
        for admin in all_admins:
            weights[admin] = equal_weight
        return weights

    if not all_admins:
        # Only owner exists
        weights[owner_id] = 1
        return weights

    # Owner exists and there are admins
    # Weight distribution:
    # - Owner gets 50% weight (or configurable)
    # - Remaining 50% split among admins
    total_weight = 1
    owner_weight = 0.5
    admin_weight_total = total_weight - owner_weight
    per_admin_weight = admin_weight_total / len(all_admins)

    # Assign weights
    weights[owner_id] = owner_weight
    for admin in all_admins:
        weights[admin] = per_admin_weight

    return weights


# -----------------------------
# Adjust weights for off-duty admins
# -----------------------------
async def apply_off_duty_modifier(weights: dict, group_id: int, off_duty_factor: float = 0.5):
    """
    Reduces the weight of off-duty admins by a factor (default 0.5).
    """
    adjusted_weights = weights.copy()
    for user_id in weights.keys():
        if await is_admin(user_id, group_id):
            on_duty = await get_on_duty_admins(group_id)
            if user_id not in on_duty:
                adjusted_weights[user_id] *= off_duty_factor
    return adjusted_weights
