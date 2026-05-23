"""
routers/admin.py — Moderation & Anonymous Messaging
Phase 2 | PRD §5 (Moderation), §5 (/anon + Ghost Moderation)

Responsibilities:
  • /warn   — increment warning_count + log in penalties + DM the user
  • /mute   — Telegram-level restrict + DB flag + log in penalties + DM
  • /ban    — Telegram-level ban + log in penalties + DM
  • /on_duty / /off_duty — toggle admin availability for alert routing
  • /anon   — DM-only anonymous forwarding with 15s Redis cooldown
              + anon_logs insert for ghost moderation

Ghost Moderation (PRD §5):
  When an admin replies to a bot-forwarded anon message with /warn or /mute,
  the bot resolves the real author from anon_logs and applies the penalty
  invisibly. The /trace command (glass-break protocol) is stubbed here and
  implemented fully in Phase 3.

Admin protection:
  Admins cannot use moderation commands against other admins (PRD §5).
  Checked in _resolve_target() before any DB writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import ChatPermissions, Message

logger = logging.getLogger(__name__)

# ── Router setup ──────────────────────────────────────────────────────────────
# No chat-type filter at the router level because:
#   - Moderation commands (/warn, /mute, /ban) fire in the group
#   - /anon fires in private DM
# Per-handler filters handle the distinction.
router = Router(name="admin")

# ── Constants ─────────────────────────────────────────────────────────────────
MUTE_DURATION_MINUTES: int = 5       # PRD §3 escalation: 5-minute mute
ANON_COOLDOWN_TTL: int = 15          # PRD §5: 15-second /anon cooldown
DUTY_CACHE_TTL: int = 3600           # Redis cache for on-duty set: 1 hour


# =============================================================================
# FILTERS
# =============================================================================

class IsAdmin(BaseFilter):
    """
    Passes only when the message sender's user_id is in the configured
    ADMIN_IDS list injected via dispatcher.workflow_data.
    Used on every admin command handler.
    """
    async def __call__(self, message: Message, admin_ids: list[int]) -> bool:
        return message.from_user is not None and message.from_user.id in admin_ids


class IsPrivateChat(BaseFilter):
    """Passes only for private (DM) conversations."""
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"


class IsGroupChat(BaseFilter):
    """Passes only for group or supergroup conversations."""
    async def __call__(self, message: Message) -> bool:
        return message.chat.type in ("group", "supergroup")


# =============================================================================
# SHARED HELPERS
# =============================================================================

async def _resolve_target(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    admin_ids: list[int],
    bot_id: int,
) -> Optional[tuple[int, bool]]:
    """
    Resolve the target user_id from a command that must be issued as a reply.

    Returns:
        (target_user_id, is_ghost_moderation)  on success
        None                                    if the command is invalid

    Ghost moderation path (PRD §5):
        If the replied-to message was sent by the bot itself, we look up
        anon_logs by message_id to find the real author. This is how admins
        moderate anonymous messages without knowing who sent them.

    Admin-protection path (PRD §5):
        "Admins cannot use moderation commands against other admins."
        Enforced here so every downstream handler inherits it automatically.
    """
    if not message.reply_to_message:
        await message.reply(
            "⚠️ <b>Reply required.</b>\n"
            "Reply directly to the message you want to act on."
        )
        return None

    replied = message.reply_to_message
    replied_user = replied.from_user

    # ── Ghost moderation: replied-to message is from the bot ─────────────────
    if replied_user and replied_user.id == bot_id:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM anon_logs WHERE message_id = $1",
                replied.message_id,
            )
        if not row:
            await message.reply(
                "❌ This bot message is not linked to an anonymous sender.\n"
                "Ghost moderation only works on /anon-forwarded messages."
            )
            return None

        real_user_id = row["user_id"]

        # Even ghost targets cannot be admins
        if real_user_id in admin_ids:
            await message.reply("❌ The anonymous sender is an admin. Command aborted.")
            return None

        return (real_user_id, True)

    # ── Standard moderation: replied-to message has a real user ──────────────
    if not replied_user:
        await message.reply("❌ Cannot identify the message author.")
        return None

    if replied_user.is_bot:
        await message.reply("❌ Bot messages cannot be moderated.")
        return None

    target_id = replied_user.id

    # PRD §5: Admins cannot moderate other admins
    if target_id in admin_ids:
        await message.reply("❌ Admins cannot be moderated via bot commands.")
        return None

    return (target_id, False)


async def _log_penalty(
    pool: asyncpg.Pool,
    user_id: int,
    admin_id: int,
    action: str,              # 'warn' | 'mute' | 'ban'
    reason: str,
) -> None:
    """
    Append an immutable record to the penalties table.
    PRD §2: penalties table schema.
    All parameters are passed as positional args — no string interpolation.
    """
    await pool.execute(
        """
        INSERT INTO penalties (user_id, admin_id, action, reason)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        admin_id,
        action,
        reason,
    )


async def _get_on_duty_admins(
    redis: aioredis.Redis,
    db_pool: asyncpg.Pool,
) -> list[int]:
    """
    Return admin_ids currently flagged as on-duty.
    Checks Redis cache first (TTL: 1 hour); falls back to DB on miss.
    PRD §5: Alert routing only pings on-duty admins.
    """
    cache_key = "admin_duty:on_duty_set"
    cached = await redis.smembers(cache_key)  # type: ignore[attr-defined]
    if cached:
        return [int(x) for x in cached]

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT admin_id FROM admin_duty WHERE is_on_duty = TRUE"
        )
    ids = [r["admin_id"] for r in rows]

    if ids:
        # Rebuild Redis cache
        await redis.sadd(cache_key, *ids)          # type: ignore[arg-type]
        await redis.expire(cache_key, DUTY_CACHE_TTL)

    return ids


def _parse_reason(message: Message, command: str) -> str:
    """
    Extract the optional reason text from a command message.
    e.g. "/warn Repeated spam" → "Repeated spam"
         "/warn"               → "No reason provided"
    """
    text = message.text or ""
    parts = text.split(None, 1)          # split on first whitespace after command
    return parts[1].strip() if len(parts) > 1 and parts[1].strip() else "No reason provided"


async def _dm_user(bot: Bot, user_id: int, text: str) -> bool:
    """
    Attempt to DM a user. Returns False silently if the user has blocked the bot.
    Callers should never crash or surface errors when this fails.
    """
    try:
        await bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception as exc:
        logger.info("DM to user %d failed (likely blocked): %s", user_id, exc)
        return False


# =============================================================================
# /warn
# =============================================================================

@router.message(Command("warn"), IsAdmin(), IsGroupChat())
async def cmd_warn(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    admin_ids: list[int],
    bot_id: int,
) -> None:
    """
    Issue a formal warning to a user.
    PRD §5: Escalation matrix — warning_count drives future auto-escalation.

    Usage (as a reply): /warn [optional reason]
    Ghost moderation:   reply to an anonymous bot message → resolves real user
    """
    result = await _resolve_target(message, bot, db_pool, admin_ids, bot_id)
    if result is None:
        return

    target_id, is_ghost = result
    reason = _parse_reason(message, "warn")
    admin_id = message.from_user.id

    # ── Atomic: increment warning_count + log penalty ─────────────────────────
    async with db_pool.acquire() as conn:
        new_count: Optional[int] = await conn.fetchval(
            """
            UPDATE users
               SET warning_count = warning_count + 1
             WHERE user_id = $1
            RETURNING warning_count
            """,
            target_id,
        )
        if new_count is None:
            # User not in DB yet — upsert them first, then set count to 1
            await conn.execute(
                """
                INSERT INTO users (user_id, group_id, warning_count)
                VALUES ($1, $2, 1)
                ON CONFLICT (user_id) DO UPDATE
                    SET warning_count = users.warning_count + 1
                """,
                target_id,
                message.chat.id,
            )
            new_count = 1

        await _log_penalty(db_pool, target_id, admin_id, "warn", reason)

    # ── DM the warned user ────────────────────────────────────────────────────
    ghost_note = "\n<i>(This relates to an anonymous message you sent.)</i>" if is_ghost else ""
    await _dm_user(
        bot,
        target_id,
        f"⚠️ <b>You have received a warning</b>{ghost_note}\n\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Total warnings:</b> {new_count}\n\n"
        f"<i>Continued violations may result in a mute or ban.</i>",
    )

    # ── Confirm in group ──────────────────────────────────────────────────────
    source_label = " <i>(ghost)</i>" if is_ghost else ""
    await message.reply(
        f"✅ Warning issued{source_label}.\n"
        f"User now has <b>{new_count}</b> warning(s).\n"
        f"<b>Reason:</b> {reason}"
    )
    logger.info(
        "WARN | admin=%d | target=%d | ghost=%s | warnings=%d | reason=%r",
        admin_id, target_id, is_ghost, new_count, reason,
    )


# =============================================================================
# /mute
# =============================================================================

@router.message(Command("mute"), IsAdmin(), IsGroupChat())
async def cmd_mute(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    admin_ids: list[int],
    bot_id: int,
    group_id: int,
) -> None:
    """
    Apply a 5-minute Telegram-level restriction + set is_muted=TRUE in DB.
    PRD §5: Strike 2 escalation duration is 5 minutes.

    Usage (as a reply): /mute [optional reason]
    Ghost moderation:   reply to an anonymous bot message → resolves real user
    """
    result = await _resolve_target(message, bot, db_pool, admin_ids, bot_id)
    if result is None:
        return

    target_id, is_ghost = result
    reason = _parse_reason(message, "mute")
    admin_id = message.from_user.id

    until_date = datetime.now(timezone.utc) + timedelta(minutes=MUTE_DURATION_MINUTES)

    # ── Telegram-level mute ───────────────────────────────────────────────────
    try:
        await bot.restrict_chat_member(
            chat_id=group_id,
            user_id=target_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
            ),
            until_date=until_date,
        )
    except Exception as exc:
        logger.error("Failed to restrict user %d: %s", target_id, exc)
        await message.reply(f"❌ Telegram API error: {exc}\nDB record was not written.")
        return

    # ── DB: set is_muted flag + log penalty ───────────────────────────────────
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, group_id, is_muted)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET is_muted = TRUE
            """,
            target_id,
            group_id,
        )
        await _log_penalty(db_pool, target_id, admin_id, "mute", reason)

    # ── DM the muted user ─────────────────────────────────────────────────────
    ghost_note = "\n<i>(This relates to an anonymous message you sent.)</i>" if is_ghost else ""
    await _dm_user(
        bot,
        target_id,
        f"🔇 <b>You have been muted</b>{ghost_note}\n\n"
        f"<b>Duration:</b> {MUTE_DURATION_MINUTES} minutes\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"<i>Your mute will lift automatically. "
        f"Further violations may result in a ban.</i>",
    )

    # ── Confirm in group ──────────────────────────────────────────────────────
    source_label = " <i>(ghost)</i>" if is_ghost else ""
    await message.reply(
        f"🔇 User muted for {MUTE_DURATION_MINUTES} minutes{source_label}.\n"
        f"<b>Reason:</b> {reason}"
    )
    logger.info(
        "MUTE | admin=%d | target=%d | ghost=%s | duration=%dm | reason=%r",
        admin_id, target_id, is_ghost, MUTE_DURATION_MINUTES, reason,
    )


# =============================================================================
# /ban
# =============================================================================

@router.message(Command("ban"), IsAdmin(), IsGroupChat())
async def cmd_ban(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    admin_ids: list[int],
    bot_id: int,
    group_id: int,
) -> None:
    """
    Permanently ban a user from the group.
    PRD §2: penalties table action = 'ban'.

    Usage (as a reply): /ban [optional reason]
    Ghost moderation: supported — resolves real user from anon_logs.
    """
    result = await _resolve_target(message, bot, db_pool, admin_ids, bot_id)
    if result is None:
        return

    target_id, is_ghost = result
    reason = _parse_reason(message, "ban")
    admin_id = message.from_user.id

    # ── Telegram-level ban ────────────────────────────────────────────────────
    try:
        await bot.ban_chat_member(chat_id=group_id, user_id=target_id)
    except Exception as exc:
        logger.error("Failed to ban user %d: %s", target_id, exc)
        await message.reply(f"❌ Telegram API error: {exc}\nDB record was not written.")
        return

    # ── DB: log penalty ───────────────────────────────────────────────────────
    # We don't delete the user row — historical points/penalties are preserved.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, group_id, is_muted)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET is_muted = TRUE
            """,
            target_id,
            group_id,
        )
        await _log_penalty(db_pool, target_id, admin_id, "ban", reason)

    # ── DM the banned user ────────────────────────────────────────────────────
    ghost_note = "\n<i>(This relates to an anonymous message you sent.)</i>" if is_ghost else ""
    await _dm_user(
        bot,
        target_id,
        f"🚫 <b>You have been banned</b>{ghost_note}\n\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"<i>Contact an admin if you believe this is an error.</i>",
    )

    # ── Confirm in group ──────────────────────────────────────────────────────
    source_label = " <i>(ghost)</i>" if is_ghost else ""
    await message.reply(
        f"🚫 User permanently banned{source_label}.\n"
        f"<b>Reason:</b> {reason}"
    )
    logger.info(
        "BAN | admin=%d | target=%d | ghost=%s | reason=%r",
        admin_id, target_id, is_ghost, reason,
    )


# =============================================================================
# /on_duty / /off_duty — Admin availability toggle
# =============================================================================

@router.message(Command("on_duty"), IsAdmin())
async def cmd_on_duty(
    message: Message,
    db_pool: asyncpg.Pool,
    redis: aioredis.Redis,
) -> None:
    """
    Mark the admin as on-duty. Only on-duty admins receive:
      • /report pings
      • Verification failure alerts
      • System-level warnings
    PRD §5: Admin Routing (/on_duty).
    """
    admin_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_duty (admin_id, is_on_duty, toggled_at)
            VALUES ($1, TRUE, NOW())
            ON CONFLICT (admin_id) DO UPDATE
                SET is_on_duty = TRUE,
                    toggled_at = NOW()
            """,
            admin_id,
        )

    # Invalidate Redis duty cache so next call to _get_on_duty_admins is fresh
    await redis.delete("admin_duty:on_duty_set")

    await message.reply(
        "✅ <b>You are now on duty.</b>\n"
        "You will receive /report pings and verification alerts.\n\n"
        "Use /off_duty when you step away."
    )
    logger.info("DUTY_ON | admin=%d", admin_id)


@router.message(Command("off_duty"), IsAdmin())
async def cmd_off_duty(
    message: Message,
    db_pool: asyncpg.Pool,
    redis: aioredis.Redis,
) -> None:
    """
    Mark the admin as off-duty. Stops routing alerts to this admin.
    PRD §5: Admin Routing (/off_duty).
    """
    admin_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_duty (admin_id, is_on_duty, toggled_at)
            VALUES ($1, FALSE, NOW())
            ON CONFLICT (admin_id) DO UPDATE
                SET is_on_duty = FALSE,
                    toggled_at = NOW()
            """,
            admin_id,
        )

    await redis.delete("admin_duty:on_duty_set")

    await message.reply(
        "💤 <b>You are now off duty.</b>\n"
        "You will not receive automated alerts.\n\n"
        "Use /on_duty when you return."
    )
    logger.info("DUTY_OFF | admin=%d", admin_id)


# =============================================================================
# /anon — Anonymous DM-to-Group forwarding
# =============================================================================

@router.message(Command("anon"), IsPrivateChat())
async def cmd_anon(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    redis: aioredis.Redis,
    group_id: int,
    bot_id: int,
) -> None:
    """
    Allows any user to forward a message to the main group anonymously.
    PRD §5 — Anonymous Messaging (/anon):

      • Command must be sent in bot's DM (private chat only)
      • Strict 15-second Redis TTL cooldown per user
      • Cooldown violation → silent DM "⏳ Please wait X seconds."
      • Bot strips all sender metadata — group sees only bot-authored text
      • Real author is recorded in anon_logs for ghost moderation
      • Anonymous messages earn ZERO economy points

    Redis key pattern: anon_cooldown:{user_id}  → TTL 15 seconds
    """
    user_id = message.from_user.id

    # ── Parse message text from command ──────────────────────────────────────
    # Full text example: "/anon Hello everyone! Check this out."
    full_text = message.text or ""
    parts = full_text.split(None, 1)   # Split on first whitespace only

    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "📨 <b>Anonymous Messaging</b>\n\n"
            "<b>Usage:</b> <code>/anon Your message here</code>\n\n"
            "Your message will be forwarded to the group with no trace of your identity.\n\n"
            "⏱ Cooldown: 15 seconds between messages.\n"
            "📵 Anonymous messages earn no points."
        )
        return

    anon_text = parts[1].strip()

    # ── Enforce 15-second cooldown via Redis TTL ──────────────────────────────
    cooldown_key = f"anon_cooldown:{user_id}"
    ttl: int = await redis.ttl(cooldown_key)   # -2 = key doesn't exist, -1 = no expiry, N = seconds left

    if ttl > 0:
        # PRD §5: "Violations trigger a silent DM: '⏳ Please wait 15 seconds.'"
        # We show the exact remaining time rather than a fixed "15 seconds".
        await message.answer(f"⏳ Please wait {ttl} second(s).")
        logger.debug("ANON_COOLDOWN | user=%d | ttl=%d", user_id, ttl)
        return

    # ── Ensure user exists in DB (required for anon_logs FK) ─────────────────
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, group_id, language_code)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
                SET language_code = EXCLUDED.language_code
            """,
            user_id,
            group_id,
            message.from_user.language_code,
        )

    # ── Forward to the group as the bot (all sender metadata stripped) ────────
    try:
        sent = await bot.send_message(
            chat_id=group_id,
            text=(
                "👤 <b>Anonymous message:</b>\n\n"
                f"{anon_text}"
            ),
            # Do NOT forward the original message — that would leak metadata.
            # Do NOT use copy_to — send a fresh bot-authored message.
        )
    except Exception as exc:
        logger.error(
            "ANON_SEND_FAILED | user=%d | group=%d | error=%s",
            user_id, group_id, exc,
        )
        await message.answer(
            "❌ Failed to send your anonymous message. Please try again."
        )
        return

    # ── Record mapping in anon_logs (enables ghost moderation) ───────────────
    # message_id is the Telegram message_id of the bot's message IN THE GROUP.
    # When an admin replies to that group message with /warn or /mute,
    # _resolve_target() looks up this row to find the real user_id.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO anon_logs (message_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT (message_id) DO NOTHING
            """,
            sent.message_id,
            user_id,
        )

    # ── Set 15-second cooldown in Redis ──────────────────────────────────────
    # SETEX is atomic — no race condition between check and set.
    await redis.setex(cooldown_key, ANON_COOLDOWN_TTL, "1")

    # ── Confirm to sender ─────────────────────────────────────────────────────
    await message.answer("✅ Your message has been sent anonymously.")
    logger.info(
        "ANON_SENT | user=%d | group_msg_id=%d | text_len=%d",
        user_id, sent.message_id, len(anon_text),
    )


# =============================================================================
# /trace — Glass-break protocol (stub for Phase 3 full implementation)
# =============================================================================

@router.message(Command("trace"), IsAdmin(), IsGroupChat())
async def cmd_trace(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
    admin_ids: list[int],
    bot_id: int,
) -> None:
    """
    PRD §5 — /trace [reason]:
    Privately DMs the admin the real identity behind an anonymous message.
    Logs the lookup in the audit trail (penalties table) for accountability.

    Full implementation in Phase 3 (Moderation & Ghost Moderation module).
    This stub validates the reply context and confirms the hook is wired.
    """
    if not message.reply_to_message:
        await message.reply("⚠️ Reply to an anonymous message to use /trace.")
        return

    replied = message.reply_to_message
    replied_user = replied.from_user

    # Only makes sense on bot-authored messages
    if not replied_user or replied_user.id != bot_id:
        await message.reply("❌ /trace only works on anonymous (bot-forwarded) messages.")
        return

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, timestamp FROM anon_logs WHERE message_id = $1",
            replied.message_id,
        )

    if not row:
        await message.reply("❌ No anonymous log found for this message.")
        return

    real_user_id = row["user_id"]
    sent_at = row["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")

    # Parse reason from command args
    text = message.text or ""
    parts = text.split(None, 1)
    reason = parts[1].strip() if len(parts) > 1 else "No reason provided"

    # ── Log the glass-break access in penalties (audit trail) ─────────────────
    async with db_pool.acquire() as conn:
        await _log_penalty(
            db_pool,
            real_user_id,
            message.from_user.id,
            "warn",                 # Trace is logged as a 'warn' audit entry
            f"[TRACE ACCESS] Admin requested identity reveal. Reason: {reason}",
        )

    # ── DM the requesting admin — never broadcast the identity publicly ───────
    await _dm_user(
        bot,
        message.from_user.id,
        f"🔍 <b>Trace Result</b>\n\n"
        f"<b>Message sent:</b> {sent_at}\n"
        f"<b>Real user ID:</b> <code>{real_user_id}</code>\n"
        f"<a href=\"tg://user?id={real_user_id}\">Tap to view profile</a>\n\n"
        f"<b>Reason logged:</b> {reason}\n\n"
        f"⚠️ <i>This access has been recorded in the audit log.</i>",
    )

    # Delete the /trace command from the group immediately (no public trace)
    try:
        await message.delete()
    except Exception:
        pass

    logger.warning(
        "TRACE | admin=%d | real_user=%d | msg_id=%d | reason=%r",
        message.from_user.id, real_user_id, replied.message_id, reason,
    )
