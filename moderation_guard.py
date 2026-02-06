"""
DO NOT DELETE
Moderation Guard Module
Eminence DAO Bot
Feb 2026

Responsibilities:
- Anti-spam & anti-link
- Enforcement rules for mute/warn/ban
- Admin protection from being moderated by other admins
- Reason logging for transparency and auditing
- /status command reporting with date & time
"""

import re
import hashlib
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

# -----------------------------
# CONFIGURABLE PARAMETERS
# -----------------------------
LINK_REGEX = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
SPAM_THRESHOLD = 5
SPAM_WINDOW = timedelta(hours=1)

# -----------------------------
# IN-MEMORY CACHE (OPTIONAL)
# -----------------------------
recent_messages = {}  # (group_id, user_id, msg_hash) -> (first_seen, count)
penalties = {}        # (group_id, user_id) -> {"level": 0=none,1=warn,2=mute,3=ban, "reason": str, "timestamp": datetime}

# -----------------------------
# HELPERS
# -----------------------------
def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())

def hash_message(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()

def contains_link(text: str) -> bool:
    return bool(LINK_REGEX.search(text))

def is_forwarded(message) -> bool:
    return bool(message.forward_from or message.forward_from_chat)

# -----------------------------
# MODERATION LOGIC
# -----------------------------
async def check_hard_violation(message) -> str | None:
    """Return reason if link or forwarded message detected."""
    if is_forwarded(message):
        return "forwarded_message"
    if contains_link(message.text or ""):
        return "link_detected"
    return None

async def check_soft_spam(message, group_id: int, user_id: int) -> bool:
    """Return True if repeated message exceeds threshold."""
    msg_hash = hash_message(message.text)
    now = datetime.utcnow()
    key = (group_id, user_id, msg_hash)

    first_seen, count = recent_messages.get(key, (now, 0))
    if now - first_seen <= SPAM_WINDOW:
        count += 1
        recent_messages[key] = (first_seen, count)
        return count >= SPAM_THRESHOLD
    else:
        recent_messages[key] = (now, 1)
        return False

async def escalate_penalty(group_id: int, user_id: int, reason: str) -> int:
    """
    Increase penalty level: 1=warn, 2=mute, 3=ban. Requires reason.
    """
    key = (group_id, user_id)
    current = penalties.get(key, {"level": 0, "reason": None, "timestamp": None})
    new_level = current["level"] + 1
    penalties[key] = {"level": new_level, "reason": reason, "timestamp": datetime.utcnow()}
    return new_level

# -----------------------------
# ADMIN SAFETY
# -----------------------------
async def can_moderate(admin_user, target_user, is_owner=False):
    """
    Check if admin can moderate the target user.
    Returns: (bool, message if cannot)
    """
    if target_user.is_bot:
        return False, "Bots cannot be moderated."
    if target_user.id == admin_user.id:
        return False, "You cannot moderate yourself."
    if target_user.status in ["administrator", "creator"] and not is_owner:
        return False, "⚠️ I cannot be involved in admin fights."
    return True, None

# -----------------------------
# COMMAND HANDLERS
# -----------------------------
async def enforce_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, target_user, reason: str, is_owner=False):
    """
    Enforce moderation action on a user.
    action: 'warn', 'mute', 'ban'
    """
    can_act, msg = await can_moderate(update.effective_user, target_user, is_owner)
    if not can_act:
        await update.message.reply_text(msg)
        return

    if not reason:
        await update.message.reply_text("❌ You must provide a reason for this action.")
        return

    # Escalate penalty
    level = await escalate_penalty(update.effective_chat.id, target_user.id, reason)

    # Notify group
    await update.message.reply_text(
        f"✅ {action.capitalize()} applied to {target_user.first_name}.\n"
        f"Reason: {reason}\n"
        f"Current penalty level: {level}\n"
        f"Date/Time: {penalties[(update.effective_chat.id, target_user.id)]['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user):
    """
    Shows whether a user is muted/banned and reason, including date & time. No IDs shown.
    """
    key = (update.effective_chat.id, target_user.id)
    info = penalties.get(key)
    if not info:
        status = "Active"
        reason = "None"
        timestamp = "N/A"
    else:
        level_map = {1: "Warned", 2: "Muted", 3: "Banned"}
        status = level_map.get(info["level"], "Active")
        reason = info.get("reason", "None")
        timestamp = info.get("timestamp")
        if timestamp:
            timestamp = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    await update.message.reply_text(
        f"Status for {target_user.first_name}:\n"
        f"State: {status}\n"
        f"Reason: {reason}\n"
        f"Date/Time: {timestamp}"
    )
