"""
Moderation Module
Eminence DAO Bot
Feb 2026

Responsibilities:
- Anti-spam & anti-link
- Captcha verification for new/rejoining users
- Escalation logic (warn → mute → ban)
- Admin protection from moderation by other admins
- Reason required for all moderation actions
- /status command reporting
- /report command
- Blacklist + AI semantic detection
- English-only enforcement (Pro+)
- Spam-score system with repeat offender ban
"""

import re
import hashlib
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from users import add_user

# -----------------------------
# CONFIGURABLE PARAMETERS
# -----------------------------
LINK_REGEX = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
SPAM_THRESHOLD = 5
SPAM_WINDOW = timedelta(hours=1)
CAPTCHA_ATTEMPTS = 5
CAPTCHA_TIMEOUT = timedelta(seconds=15)
SPAM_FIRST_MUTE = 5   # minutes
AI_CONF_THRESHOLD = 0.65
ENGLISH_WARN_THRESHOLD = 5

# -----------------------------
# IN-MEMORY CACHE
# -----------------------------
recent_messages = {}        # (group_id, user_id, msg_hash) -> (first_seen, count)
spam_offenses = {}          # (group_id, user_id) -> offense count
penalties = {}              # (group_id, user_id) -> {"level":0=none,1=warn,2=mute,3=ban, "reason":str, "timestamp":datetime}
captcha_challenges = {}     # user_id -> {"answer":int, "attempts":int, "expires":datetime}
reports = []                # list of report dicts
group_settings = {}         # group_id -> settings dict

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
# ADMIN SAFETY
# -----------------------------
async def can_moderate(actor, target_user, is_owner=False):
    if target_user.is_bot:
        return False, "Bots cannot be moderated."
    if target_user.id == actor.id:
        return False, "You cannot moderate yourself."
    if target_user.status in ["administrator", "creator"] and not is_owner:
        return False, "⚠️ Cannot moderate fellow admins/owners."
    return True, None

# -----------------------------
# PENALTY ESCALATION
# -----------------------------
async def escalate_penalty(group_id: int, user_id: int, reason: str) -> int:
    key = (group_id, user_id)
    current = penalties.get(key, {"level": 0})
    new_level = current["level"] + 1
    penalties[key] = {"level": new_level, "reason": reason, "timestamp": datetime.utcnow()}
    return new_level

# -----------------------------
# SPAM SCORE / DUPLICATE HANDLER
# -----------------------------
async def handle_duplicate_spam(group_id: int, user_id: int, msg_hash: str, message_obj):
    now = datetime.utcnow()
    key = (group_id, user_id, msg_hash)
    first_seen, count = recent_messages.get(key, (now, 0))
    if now - first_seen <= SPAM_WINDOW:
        count += 1
        recent_messages[key] = (first_seen, count)
        if count >= SPAM_THRESHOLD:
            # First offense → mute
            if spam_offenses.get((group_id, user_id), 0) == 0:
                spam_offenses[(group_id, user_id)] = 1
                await message_obj.delete()
                await mute_user(message_obj.chat_id, user_id, SPAM_FIRST_MUTE)
                return "first_mute"
            # Second offense → ban
            elif spam_offenses.get((group_id, user_id), 0) == 1:
                spam_offenses[(group_id, user_id)] = 2
                await message_obj.delete()
                await ban_user(message_obj.chat_id, user_id)
                return "second_ban"
    else:
        recent_messages[key] = (now, 1)
    return None

# -----------------------------
# MODERATION GUARD
# -----------------------------
async def moderation_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.is_bot:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Duplicate spam
    msg_hash = hash_message(text)
    spam_result = await handle_duplicate_spam(chat_id, user.id, msg_hash, update.message)
    if spam_result:
        return

    # Blacklist check
    if await is_blacklisted(text, chat_id):
        await update.message.delete()
        await mute_user(chat_id, user.id, 5, reason="Blacklist word")
        return

    # AI Semantic Blacklist (Pro+)
    settings = group_settings.get(chat_id, {})
    ai_enabled = settings.get("ai_enabled", False)
    user_tier = settings.get("tier", "free")
    if ai_enabled and user_tier in ["pro", "pro+", "enterprise"]:
        ai_result = await analyze_ai_blacklist(text, chat_id)
        if ai_result and ai_result.get("confidence", 0) >= AI_CONF_THRESHOLD:
            await update.message.delete()
            await mute_user(chat_id, user.id, 5, reason=f"AI detected: {ai_result['concept']}")
            return

    # English-only enforcement (Pro+)
    english_only = settings.get("english_only", False)
    if english_only and user_tier in ["pro", "pro+", "enterprise"]:
        lang = await detect_language(text)
        if lang != "en":
            level = await escalate_penalty(chat_id, user.id, "Non-English message in English-only group")
            await update.message.delete()
            warning_text = await get_translated_warning(lang)
            await update.message.reply_text(warning_text)
            if level >= ENGLISH_WARN_THRESHOLD:
                await mute_user(chat_id, user.id, 5, reason="Repeated non-English messages")
            return

# -----------------------------
# REGISTER HANDLERS
# -----------------------------
def register_moderation_handlers(app):
    from moderation_actions import mute, ban  # manual commands
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("dmute", mute))  # delete + mute wrapper
    app.add_handler(CommandHandler("dban", ban))    # delete + ban wrapper
    app.add_handler(CommandHandler("warn", warn))   # manual warn
    app.add_handler(CommandHandler("kick", kick))   # manual kick
    app.add_handler(MessageHandler(filters.ALL, moderation_guard))
