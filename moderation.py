"""
Moderation Module
Eminence DAO Bot
Feb 2026

Responsibilities:
- Anti-spam & anti-link
- Captcha verification for new/rejoining users
- Cross-group banning & reputation tracking
- Escalation logic (warn → mute → ban)
- Admin protection from moderation by other admins
- Reason required for all moderation actions
- /status command reporting
- /report command
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
CROSS_GROUP_BAN_THRESHOLD = 3  # banned in 3 groups → global restriction
CAPTCHA_ATTEMPTS = 5
CAPTCHA_TIMEOUT = timedelta(seconds=15)

# -----------------------------
# IN-MEMORY CACHE
# -----------------------------
recent_messages = {}   # (group_id, user_id, msg_hash) -> (first_seen, count)
penalties = {}         # (group_id, user_id) -> {"level":0=none,1=warn,2=mute,3=ban, "reason":str, "timestamp":datetime}
cross_group_bans = {}  # user_id -> set(group_ids)
reports = []           # list of report dicts
captcha_challenges = {} # user_id -> {"answer":int, "attempts":int, "expires":datetime}

group_settings = {}    # group_id -> settings dict

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

def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# -----------------------------
# CAPTCHA
# -----------------------------
def generate_captcha():
    import random
    a, b = random.randint(1, 10), random.randint(1, 10)
    return f"{a} + {b}", a + b

async def create_captcha(user_id):
    question, answer = generate_captcha()
    captcha_challenges[user_id] = {
        "answer": answer,
        "attempts": 0,
        "expires": datetime.utcnow() + CAPTCHA_TIMEOUT
    }
    return question

async def validate_captcha(user_id, response: str):
    challenge = captcha_challenges.get(user_id)
    if not challenge:
        return False, "No active captcha."
    if datetime.utcnow() > challenge["expires"]:
        del captcha_challenges[user_id]
        return False, "Captcha expired."
    try:
        if int(response) == challenge["answer"]:
            del captcha_challenges[user_id]
            return True, "Captcha solved!"
    except ValueError:
        pass
    challenge["attempts"] += 1
    if challenge["attempts"] >= CAPTCHA_ATTEMPTS:
        del captcha_challenges[user_id]
        return False, "Too many incorrect attempts."
    return False, "Incorrect, try again."

# -----------------------------
# ANTI-SPAM / ANTI-LINK
# -----------------------------
async def check_hard_violation(message) -> str | None:
    if is_forwarded(message):
        return "forwarded_message"
    if contains_link(message.text or ""):
        return "link_detected"
    return None

async def check_soft_spam(message, group_id: int, user_id: int) -> bool:
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

# -----------------------------
# ESCALATION LOGIC
# -----------------------------
async def escalate_penalty(group_id: int, user_id: int, reason: str) -> int:
    key = (group_id, user_id)
    current = penalties.get(key, {"level": 0})
    new_level = current["level"] + 1
    penalties[key] = {"level": new_level, "reason": reason, "timestamp": datetime.utcnow()}

    if new_level >= 3:
        cross_group_bans.setdefault(user_id, set()).add(group_id)
    return new_level

async def check_cross_group_restriction(user_id: int) -> bool:
    return len(cross_group_bans.get(user_id, set())) >= CROSS_GROUP_BAN_THRESHOLD

# -----------------------------
# ADMIN SAFETY
# -----------------------------
async def can_moderate(admin_user, target_user, is_owner=False):
    if target_user.is_bot:
        return False, "Bots cannot be moderated."
    if target_user.id == admin_user.id:
        return False, "You cannot moderate yourself."
    if target_user.status in ["administrator", "creator"] and not is_owner:
        return False, "⚠️ Cannot moderate fellow admins/owners."
    return True, None

# -----------------------------
# MODERATION ACTION HANDLER
# -----------------------------
async def enforce_action(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         action: str, target_user, reason: str, is_owner=False):

    can_act, msg = await can_moderate(update.effective_user, target_user, is_owner)
    if not can_act:
        await update.message.reply_text(msg)
        return

    if not reason:
        await update.message.reply_text("❌ You must provide a reason for this action.")
        return

    level = await escalate_penalty(update.effective_chat.id, target_user.id, reason)
    timestamp = penalties[(update.effective_chat.id, target_user.id)]["timestamp"]

    await update.message.reply_text(
        f"✅ {action.capitalize()} applied to {target_user.first_name}\n"
        f"Reason: {reason}\n"
        f"Penalty Level: {level}\n"
        f"Date/Time: {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

# -----------------------------
# STATUS COMMAND
# -----------------------------
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user):
    key = (update.effective_chat.id, target_user.id)
    info = penalties.get(key)

    if not info:
        await update.message.reply_text(
            f"Status for {target_user.first_name}:\n"
            f"State: Active\nReason: None\nDate/Time: N/A"
        )
        return

    level_map = {1: "Warned", 2: "Muted", 3: "Banned"}
    await update.message.reply_text(
        f"Status for {target_user.first_name}:\n"
        f"State: {level_map.get(info['level'], 'Active')}\n"
        f"Reason: {info['reason']}\n"
        f"Date/Time: {info['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

# -----------------------------
# REPORT COMMAND
# -----------------------------
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ You must reply to a message to report it.")
        return

    chat = update.effective_chat
    reporter = update.effective_user
    target = update.message.reply_to_message.from_user
    msg = update.message.reply_to_message

    is_target_admin = target.status in ["administrator", "creator"]
    allow_admin_reports = group_settings.get(chat.id, {}).get("allow_admin_reports", False)

    report = {
        "group_id": chat.id,
        "reported_user": target.id,
        "reported_name": target.full_name,
        "reporter": reporter.id,
        "message": msg.text or "[NON-TEXT MESSAGE]",
        "timestamp": utc_now(),
        "message_id": msg.message_id
    }

    reports.append(report)

    if is_target_admin and not allow_admin_reports:
        await update.message.reply_text("❌ Admins cannot be reported in this group.")
        return

    await update.message.reply_text("✅ Report submitted. Moderators have been notified.")
