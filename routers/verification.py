"""
routers/verification.py
Phase 3 — Verification Flow & Anti-Spam
Handles: new-member mute → DM rules lecture → DM quiz → 3-strike lockout → unlock + DB log
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, CommandStart
from aiogram.filters.chat_member_updated import IS_NOT_MEMBER, MEMBER, RESTRICTED
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

log = logging.getLogger(__name__)
router = Router()

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------
DAO_GROUP_ID: int = -1001234567890   # Your main group chat_id
BOT_USERNAME: str = "YourBotUsername"  # Without @

# Redis key helpers
def _rkey_attempts(uid: int) -> str:
    return f"verify:attempts:{uid}"

def _rkey_locked(uid: int) -> str:
    return f"verify:locked:{uid}"

def _rkey_welcome_msg(uid: int) -> str:
    return f"verify:welcome_msg:{uid}"

def _rkey_spam_hash(uid: int, h: str) -> str:
    return f"spam:hash:{uid}:{h}"

def _rkey_spam_strike(uid: int) -> str:
    return f"spam:strike:{uid}"

def _rkey_anon_cooldown(uid: int) -> str:
    return f"anon:cooldown:{uid}"

# ---------------------------------------------------------------------------
# Rules & Quiz definition
# ---------------------------------------------------------------------------
DAO_RULES_TEXT = (
    "📜 *Acki Nacki DAO — Core Rules*\n\n"
    "Before you enter the Safe Zone, you must agree to our constitution:\n\n"
    "*1. Protect the Safe Zone:* No harassment, doxing, or hate speech. We are here to connect.\n"
    "*2. Speak With Substance:* Messages must be at least 15 characters to earn mining points.\n"
    "*3. Respect the Veil:* Using /anon to troll or insult will result in an immediate ban.\n"
    "*4. Presence is Required:* 90 days of complete inactivity = automatic removal.\n"
    "*5. No Spam or Begging:* Unapproved links and begging for tokens are strictly banned.\n"
    "*6. English Only:* To ensure our team can effectively protect this space, all chat must be in English.\n"
)

QUIZ: list[dict] = [
    {
        "q": "📖 *Question 1 of 3*\n\nWhat is the minimum character count for a text message to earn *1.0 full point* in the DAO economy?",
        "options": [
            ("A) 10 characters", False),
            ("B) 15 characters", True),
            ("C) 20 characters", False),
            ("D) No minimum", False),
        ],
    },
    {
        "q": "📖 *Question 2 of 3*\n\nWhat is the *maximum* number of points a member can earn per day (the Daily Peg)?",
        "options": [
            ("A) 500 points", False),
            ("B) 1,000 points", False),
            ("C) 700 points", True),
            ("D) Unlimited", False),
        ],
    },
    {
        "q": "📖 *Question 3 of 3*\n\nWhat language must all main-chat messages be written in?",
        "options": [
            ("A) Any language is fine", False),
            ("B) English only", True),
            ("C) English or Spanish", False),
            ("D) The bot decides", False),
        ],
    },
]

MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 86_400   # 24 hours
SPAM_WINDOW_SECONDS = 3_600  # 1-hour rolling window
SPAM_THRESHOLD = 5
ANON_COOLDOWN_SECONDS = 15


def _build_quiz_keyboard(question_idx: int) -> InlineKeyboardMarkup:
    """Build an inline keyboard for a quiz question."""
    q = QUIZ[question_idx]
    buttons = [
        [InlineKeyboardButton(
            text=label,
            callback_data=f"quiz:{question_idx}:{int(correct)}"
        )]
        for label, correct in q["options"]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 1. New-member join — restrict & send deep-link
# ---------------------------------------------------------------------------
@router.chat_member(
    ChatMemberUpdatedFilter(member_status_changed=(IS_NOT_MEMBER | RESTRICTED) >> MEMBER)
)
async def on_new_member(
    event: ChatMemberUpdated,
    bot: Bot,
    pool,       
    redis,      
):
    if event.chat.id != DAO_GROUP_ID:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return

    # --- Restrict immediately ---
    no_perms = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    try:
        await bot.restrict_chat_member(DAO_GROUP_ID, user.id, permissions=no_perms)
    except Exception as exc:
        log.warning("Could not restrict user %d: %s", user.id, exc)
        return

    # --- Send welcome with deep-link button ---
    deep_link = f"https://t.me/{BOT_USERNAME}?start=verify_{DAO_GROUP_ID}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Verify Me", url=deep_link)
    ]])
    welcome = await bot.send_message(
        DAO_GROUP_ID,
        f"👋 Welcome, {user.mention_html()}!\n\n"
        "Before you can chat, please verify you've read the rules.\n"
        "Tap the button below to start your verification in my DM.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    # Store welcome message ID so we can delete it after passing
    await redis.setex(_rkey_welcome_msg(user.id), 86_400 * 2, str(welcome.message_id))


# ---------------------------------------------------------------------------
# 2. /start verify — Lecture step (Show Rules)
# ---------------------------------------------------------------------------
@router.message(CommandStart(deep_link=True, deep_link_encoded=False), F.chat.type == "private")
async def on_start_verify(
    message: Message,
    bot: Bot,
    redis,
    command: Command = None,
):
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    if not payload.startswith("verify_"):
        return

    user_id = message.from_user.id

    # Check lockout
    locked = await redis.get(_rkey_locked(user_id))
    if locked:
        ttl = await redis.ttl(_rkey_locked(user_id))
        hours = ttl // 3600
        mins = (ttl % 3600) // 60
        await message.answer(
            f"🔒 You are locked out for {hours}h {mins}m due to repeated quiz failures.\n"
            "Contact an admin if you believe this is an error."
        )
        return

    # Present the rules before the quiz
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ I have read the rules", callback_data="rules_read")
    ]])
    
    await message.answer(
        DAO_RULES_TEXT,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# 3. Rules Read Callback — Start Quiz Q1
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "rules_read")
async def on_rules_read(
    call: CallbackQuery,
    bot: Bot,
    redis,
):
    user_id = call.from_user.id
    
    # Reset attempt counter for fresh session
    await redis.delete(_rkey_attempts(user_id))

    await call.message.edit_text(
        "🧠 *Acki Nacki DAO — Verification Quiz*\n\n"
        "You have *3 attempts*. Read carefully!\n\n"
        + QUIZ[0]["q"],
        reply_markup=_build_quiz_keyboard(0),
        parse_mode="Markdown",
    )
    await call.answer()

# ---------------------------------------------------------------------------
# 4. Quiz callback handler
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("quiz:"))
async def on_quiz_answer(
    call: CallbackQuery,
    bot: Bot,
    pool,
    redis,
):
    _, q_idx_str, correct_str = call.data.split(":")
    q_idx = int(q_idx_str)
    is_correct = correct_str == "1"
    user = call.from_user
    user_id = user.id

    # --- Wrong answer ---
    if not is_correct:
        raw_attempts = await redis.get(_rkey_attempts(user_id))
        attempts = int(raw_attempts) + 1 if raw_attempts else 1
        await redis.setex(_rkey_attempts(user_id), 86_400, str(attempts))

        remaining = MAX_ATTEMPTS - attempts
        if remaining <= 0:
            await _handle_lockout(call, bot, redis, user)
            return

        await call.answer("❌ Incorrect. Try again.", show_alert=True)
        # Re-send same question
        await call.message.edit_text(
            f"❌ Wrong answer. You have *{remaining}* attempt(s) left.\n\n" + QUIZ[q_idx]["q"],
            reply_markup=_build_quiz_keyboard(q_idx),
            parse_mode="Markdown",
        )
        return

    # --- Correct ---
    await call.answer("✅ Correct!", show_alert=False)
    next_q = q_idx + 1

    if next_q < len(QUIZ):
        # Advance to next question
        await call.message.edit_text(
            "✅ Correct!\n\n" + QUIZ[next_q]["q"],
            reply_markup=_build_quiz_keyboard(next_q),
            parse_mode="Markdown",
        )
        return

    # --- All questions passed ---
    await _handle_verification_passed(call, bot, pool, redis, user)


# ---------------------------------------------------------------------------
# Helper: lockout
# ---------------------------------------------------------------------------
async def _handle_lockout(
    call: CallbackQuery,
    bot: Bot,
    redis,
    user,
):
    user_id = user.id

    # Lock user for 24 h
    await redis.setex(_rkey_locked(user_id), LOCKOUT_SECONDS, "1")
    await redis.delete(_rkey_attempts(user_id))

    await call.message.edit_text(
        "🔒 You have failed the quiz 3 times.\n"
        "You are locked out for *24 hours*. An admin has been notified.\n\n"
        "If this is a language issue, an admin can manually verify you.",
        parse_mode="Markdown",
    )

    # Alert on-duty admins
    lang = user.language_code or "unknown"
    lang_note = (
        f"⚠️ Language barrier possible — Telegram lang: `{lang}`"
        if lang != "en"
        else ""
    )
    alert_text = (
        f"🚨 *Verification Lockout*\n"
        f"User: {user.mention_markdown()} (`{user_id}`)\n"
        f"Failed quiz 3 times.\n{lang_note}\n\n"
        f"Use `/unmute {user_id}` to manually override."
    )

    on_duty: list[bytes] = await redis.smembers("admin:on_duty")
    for admin_id_bytes in on_duty:
        try:
            await bot.send_message(
                int(admin_id_bytes),
                alert_text,
                parse_mode="Markdown",
            )
        except Exception as exc:
            log.warning("Could not DM admin %s: %s", admin_id_bytes, exc)


# ---------------------------------------------------------------------------
# Helper: verification passed
# ---------------------------------------------------------------------------
async def _handle_verification_passed(
    call: CallbackQuery,
    bot: Bot,
    pool,
    redis,
    user,
):
    user_id = user.id

    # 1. Restore permissions
    full_perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await bot.restrict_chat_member(DAO_GROUP_ID, user_id, permissions=full_perms)
    except Exception as exc:
        log.error("Failed to restore perms for %d: %s", user_id, exc)

    # 2. Delete welcome message from main group
    welcome_msg_id_raw = await redis.get(_rkey_welcome_msg(user_id))
    if welcome_msg_id_raw:
        try:
            await bot.delete_message(DAO_GROUP_ID, int(welcome_msg_id_raw))
        except Exception:
            pass  
        await redis.delete(_rkey_welcome_msg(user_id))

    # 3. Upsert into users table
    lang_code = user.language_code or "en"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, group_id, language_code, join_date)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
                SET group_id = EXCLUDED.group_id,
                    language_code = EXCLUDED.language_code
            """,
            user_id,
            DAO_GROUP_ID,
            lang_code,
            datetime.now(tz=timezone.utc),
        )

    # 4. Clean up Redis state
    await redis.delete(_rkey_attempts(user_id))
    await redis.delete(_rkey_locked(user_id))

    # 5. Confirm to user
    await call.message.edit_text(
        "🎉 *Verification complete!*\n\n"
        "Welcome to the Acki Nacki DAO community. "
        "You can now send messages in the group. Remember:\n"
        "• English only in the main chat\n"
        "• Reply to earn Spark points\n"
        "• Max 700 points/day\n\n"
        "Let's build together. 🐝",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# 5. Anti-Spam — 5-in-1-hour hash rule (group messages)
# ---------------------------------------------------------------------------
@router.message(F.chat.id == DAO_GROUP_ID, F.text)
async def anti_spam_hash_check(
    message: Message,
    bot: Bot,
    pool,
    redis,
):
    """
    Strip + lowercase → SHA-256 → check count in rolling 1-hour Redis window.
    Strike 1 → warn. Strike 2 → 5-min mute.
    Runs on group text messages. Place BEFORE economy router in include order
    so spam is caught before points are awarded.
    """
    if message.from_user.is_bot:
        return

    user_id = message.from_user.id
    clean = message.text.replace(" ", "").lower()
    h = hashlib.sha256(clean.encode()).hexdigest()[:16]  # 16-char prefix is unique enough

    hash_key = _rkey_spam_hash(user_id, h)
    count_raw = await redis.get(hash_key)
    count = int(count_raw) + 1 if count_raw else 1

    if count == 1:
        await redis.setex(hash_key, SPAM_WINDOW_SECONDS, str(count))
    else:
        await redis.setex(hash_key, await redis.ttl(hash_key) or SPAM_WINDOW_SECONDS, str(count))

    if count < SPAM_THRESHOLD:
        return  # Under threshold — pass through

    # Threshold hit — escalation
    strike_key = _rkey_spam_strike(user_id)
    strike_raw = await redis.get(strike_key)
    strike = int(strike_raw) + 1 if strike_raw else 1
    await redis.setex(strike_key, SPAM_WINDOW_SECONDS, str(strike))

    try:
        await message.delete()
    except Exception:
        pass

    if strike == 1:
        # Strike 1: warn
        warn_msg = await bot.send_message(
            DAO_GROUP_ID,
            f"⚠️ {message.from_user.mention_html()} — Spam detected. "
            "Please do not repeat the same message.",
            parse_mode="HTML",
        )
        # Log to DB
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id, 0, "warn", "Auto: spam hash threshold (Strike 1)",
                datetime.now(tz=timezone.utc),
            )
            await conn.execute(
                "UPDATE users SET warning_count = warning_count + 1 WHERE user_id = $1",
                user_id,
            )
        await asyncio.sleep(5)
        try:
            await warn_msg.delete()
        except Exception:
            pass

    elif strike >= 2:
        # Strike 2+: 5-minute mute
        mute_perms = ChatPermissions(can_send_messages=False)
        try:
            await bot.restrict_chat_member(DAO_GROUP_ID, user_id, permissions=mute_perms)
        except Exception as exc:
            log.warning("Could not mute spammer %d: %s", user_id, exc)
            return

        mute_msg = await bot.send_message(
            DAO_GROUP_ID,
            f"🔇 {message.from_user.mention_html()} has been muted for 5 minutes "
            "due to repeated spam.",
            parse_mode="HTML",
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id, 0, "mute", "Auto: spam hash threshold (Strike 2)",
                datetime.now(tz=timezone.utc),
            )
            await conn.execute(
                "UPDATE users SET is_muted = TRUE WHERE user_id = $1", user_id
            )

        await asyncio.sleep(300)  # 5-min mute served
        full_perms = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
        try:
            await bot.restrict_chat_member(DAO_GROUP_ID, user_id, permissions=full_perms)
            await conn.execute(
                "UPDATE users SET is_muted = FALSE WHERE user_id = $1", user_id
            )
        except Exception:
            pass

        try:
            await mute_msg.delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. /anon command — private DM only
# ---------------------------------------------------------------------------
@router.message(Command("anon"), F.chat.type == "private")
async def anon_command(
    message: Message,
    bot: Bot,
    pool,
    redis,
):
    user_id = message.from_user.id
    text = message.text

    # Strip /anon prefix
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: `/anon Your message here`", parse_mode="Markdown")
        return

    anon_text = parts[1].strip()

    # Cooldown check
    cooldown_key = _rkey_anon_cooldown(user_id)
    if await redis.exists(cooldown_key):
        await message.answer("⏳ Please wait 15 seconds between anonymous messages.")
        return

    # Forward to group
    sent = await bot.send_message(
        DAO_GROUP_ID,
        f"👻 *Anonymous message:*\n\n{anon_text}",
        parse_mode="Markdown",
    )

    # Log to anon_logs for /trace capability
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO anon_logs (message_id, user_id, timestamp)
            VALUES ($1, $2, $3)
            ON CONFLICT (message_id) DO NOTHING
            """,
            sent.message_id,
            user_id,
            datetime.now(tz=timezone.utc),
        )

    # Set cooldown
    await redis.setex(cooldown_key, ANON_COOLDOWN_SECONDS, "1")
    await message.answer("✅ Your anonymous message has been sent.")

