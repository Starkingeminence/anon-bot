"""
Subscription Manager
Eminence DAO Bot
Handles:
- Phase transition detection
- Owner DM notifications
- Restart-safe lifecycle tracking
"""

import asyncio
from connection import db


# ---------------------------------------
# PHASE → OWNER MESSAGE MAPPING
# ---------------------------------------

PHASE_MESSAGES = {
    "grace": (
        "⚠️ Your subscription has expired.\n\n"
        "Grace period is now active. All features still work.\n"
        "Please upgrade before grace ends to avoid degradation."
    ),

    "phase1": (
        "⚠️ Grace period has ended.\n\n"
        "Phase 1 Activated:\n"
        "• AI features have been disabled.\n\n"
        "Upgrade to restore full functionality."
    ),

    "phase2": (
        "⚠️ Phase 2 Activated:\n\n"
        "• English-only enforcement disabled\n"
        "• Games disabled\n\n"
        "Upgrade to prevent further degradation."
    ),

    "phase3": (
        "⚠️ Phase 3 Activated:\n\n"
        "• Advanced spam & moderation disabled\n"
        "Bot now running in basic protection mode.\n\n"
        "Upgrade soon to avoid full shutdown."
    ),

    "dormant": (
        "🚫 Bot is now dormant.\n\n"
        "All functionality has stopped.\n\n"
        "Use /upgrade to reactivate your subscription."
    )
}


# ---------------------------------------
# GET ALL GROUPS WITH SUBSCRIPTIONS
# ---------------------------------------

async def get_all_groups():
    return await db.fetch("SELECT group_id, last_notified_phase FROM group_subscriptions")


# ---------------------------------------
# GET OWNER ID
# ---------------------------------------
# Assumes you have a groups table storing owner_id.
# If not, modify this query accordingly.

async def get_group_owner(group_id: int):
    row = await db.fetchrow(
        "SELECT owner_id FROM groups WHERE group_id = $1",
        group_id
    )
    return row["owner_id"] if row else None


# ---------------------------------------
# GET CURRENT PHASE
# ---------------------------------------

async def get_current_phase(group_id: int):
    row = await db.fetchrow(
        "SELECT get_subscription_status($1) AS status",
        group_id
    )
    if not row:
        return "active"
    return row["status"]


# ---------------------------------------
# UPDATE LAST NOTIFIED PHASE
# ---------------------------------------

async def update_last_notified_phase(group_id: int, phase: str):
    await db.execute(
        """
        UPDATE group_subscriptions
        SET last_notified_phase = $1,
            updated_at = NOW()
        WHERE group_id = $2
        """,
        phase,
        group_id
    )


# ---------------------------------------
# MAIN WATCHER LOOP
# ---------------------------------------

async def subscription_phase_watcher(app):
    """
    Runs forever.
    Checks subscription phase transitions.
    Sends DM to owner once per phase.
    """

    while True:
        try:
            groups = await get_all_groups()

            for row in groups:
                group_id = row["group_id"]
                last_phase = row["last_notified_phase"]

                current_phase = await get_current_phase(group_id)

                # Only notify if:
                # - Phase changed
                # - Phase is not active
                if current_phase != last_phase and current_phase in PHASE_MESSAGES:

                    owner_id = await get_group_owner(group_id)
                    if not owner_id:
                        continue

                    message = PHASE_MESSAGES[current_phase]

                    try:
                        await app.bot.send_message(
                            chat_id=owner_id,
                            text=message
                        )

                        # Update last notified phase
                        await update_last_notified_phase(group_id, current_phase)

                    except Exception:
                        # Owner may have blocked bot
                        pass

        except Exception:
            # Prevent task from dying
            pass

        # Run every 1 hour
        await asyncio.sleep(3600)
