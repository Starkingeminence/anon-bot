"""
routers/admin.py
Phase 2 (existing) + Phase 3 update: fully implemented /trace glass-break protocol.
Only the /trace handler and its helpers are new/changed — all other Phase 2
handlers are preserved unchanged.
"""

import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

log = logging.getLogger(__name__)
router = Router()

# ---------------------------------------------------------------------------
# Shared config — must match verification.py
# ---------------------------------------------------------------------------
import os

DAO_GROUP_ID: int = int(os.getenv("GROUP_ID", "0"))

# Pull the admin IDs from your .env file
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
}


# ---------------------------------------------------------------------------
# Guard: ensure command caller is a real admin
# ---------------------------------------------------------------------------
async def _is_admin(user_id: int, bot: Bot, chat_id: int) -> bool:
    """
    Double-checks Telegram's own admin list so ADMIN_IDS is a secondary
    safeguard only. Prevents privilege-escalation via data tampering.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# /on_duty — toggle admin availability
# ---------------------------------------------------------------------------
@router.message(Command("on_duty"), F.chat.id == DAO_GROUP_ID)
async def cmd_on_duty(message: Message, bot: Bot, redis):
    user_id = message.from_user.id
    if not await _is_admin(user_id, bot, DAO_GROUP_ID):
        return

    await redis.sadd("admin:on_duty", str(user_id))
    try:
        await message.delete()
    except Exception:
        pass
    await bot.send_message(
        user_id,
        "✅ You are now *on duty*. You will receive /report pings and system alerts.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /off_duty
# ---------------------------------------------------------------------------
@router.message(Command("off_duty"), F.chat.id == DAO_GROUP_ID)
async def cmd_off_duty(message: Message, bot: Bot, redis):
    user_id = message.from_user.id
    if not await _is_admin(user_id, bot, DAO_GROUP_ID):
        return

    await redis.srem("admin:on_duty", str(user_id))
    try:
        await message.delete()
    except Exception:
        pass
    await bot.send_message(
        user_id,
        "🔕 You are now *off duty*.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /warn — works on normal messages AND anonymous messages (ghost moderation)
# ---------------------------------------------------------------------------
@router.message(Command("warn"), F.chat.id == DAO_GROUP_ID, F.reply_to_message)
async def cmd_warn(message: Message, bot: Bot, pool, redis):
    admin_id = message.from_user.id
    if not await _is_admin(admin_id, bot, DAO_GROUP_ID):
        return

    target_msg = message.reply_to_message

    # Resolve real user — either direct sender or anon_log lookup
    real_user_id = await _resolve_target(target_msg, pool)
    if not real_user_id:
        await message.answer("⚠️ Could not identify the message author.")
        return

    # Admins cannot warn other admins
    if await _is_admin(real_user_id, bot, DAO_GROUP_ID):
        await message.answer("🚫 You cannot use moderation commands against admins.")
        return

    args = message.text.split(maxsplit=1)
    reason = args[1] if len(args) > 1 else "No reason provided."

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
            VALUES ($1, $2, $3, $4, $5)
            """,
            real_user_id, admin_id, "warn", reason,
            datetime.now(tz=timezone.utc),
        )
        await conn.execute(
            "UPDATE users SET warning_count = warning_count + 1 WHERE user_id = $1",
            real_user_id,
        )

    try:
        await bot.send_message(
            real_user_id,
            f"⚠️ You have received a warning from a moderator.\n*Reason:* {reason}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    try:
        await message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /mute — ghost moderation aware
# ---------------------------------------------------------------------------
@router.message(Command("mute"), F.chat.id == DAO_GROUP_ID, F.reply_to_message)
async def cmd_mute(message: Message, bot: Bot, pool, redis):
    from aiogram.types import ChatPermissions

    admin_id = message.from_user.id
    if not await _is_admin(admin_id, bot, DAO_GROUP_ID):
        return

    target_msg = message.reply_to_message
    real_user_id = await _resolve_target(target_msg, pool)
    if not real_user_id:
        await message.answer("⚠️ Could not identify the message author.")
        return

    if await _is_admin(real_user_id, bot, DAO_GROUP_ID):
        await message.answer("🚫 You cannot mute other admins.")
        return

    args = message.text.split(maxsplit=1)
    reason = args[1] if len(args) > 1 else "No reason provided."

    mute_perms = ChatPermissions(can_send_messages=False)
    try:
        await bot.restrict_chat_member(DAO_GROUP_ID, real_user_id, permissions=mute_perms)
    except Exception as exc:
        log.warning("Could not mute %d: %s", real_user_id, exc)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
            VALUES ($1, $2, $3, $4, $5)
            """,
            real_user_id, admin_id, "mute", reason,
            datetime.now(tz=timezone.utc),
        )
        await conn.execute(
            "UPDATE users SET is_muted = TRUE WHERE user_id = $1", real_user_id
        )

    try:
        await bot.send_message(
            real_user_id,
            f"🔇 You have been muted by a moderator.\n*Reason:* {reason}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    try:
        await message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /report — reply-lock enforced
# ---------------------------------------------------------------------------
@router.message(Command("report"), F.chat.id == DAO_GROUP_ID)
async def cmd_report(message: Message, bot: Bot, pool, redis):
    if not message.reply_to_message:
        await message.answer(
            "⚠️ `/report` must be used as a reply to the offending message.",
            parse_mode="Markdown",
        )
        return

    target_msg = message.reply_to_message
    reporter_id = message.from_user.id

    args = message.text.split(maxsplit=1)
    reason = args[1] if len(args) > 1 else "No reason provided."

    # Snapshot to DB immediately (prevents evidence deletion)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
            VALUES ($1, $2, $3, $4, $5)
            """,
            target_msg.from_user.id if target_msg.from_user else 0,
            reporter_id,
            "warn",
            f"[REPORT] {reason} | Original text: {(target_msg.text or '')[:300]}",
            datetime.now(tz=timezone.utc),
        )

    # Ping on-duty admins
    on_duty: set[bytes] = await redis.smembers("admin:on_duty")
    alert = (
        f"🚨 *Report received*\n"
        f"Reporter: `{reporter_id}`\n"
        f"Reported message ID: `{target_msg.message_id}`\n"
        f"Reason: {reason}"
    )
    for admin_id_bytes in on_duty:
        try:
            await bot.send_message(int(admin_id_bytes), alert, parse_mode="Markdown")
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass

    await bot.send_message(
        reporter_id,
        "✅ Your report has been logged and forwarded to on-duty admins.",
    )


# ---------------------------------------------------------------------------
# /unmute — manual admin override (used after verification lockout)
# ---------------------------------------------------------------------------
@router.message(Command("unmute"), F.chat.id == DAO_GROUP_ID)
async def cmd_unmute(message: Message, bot: Bot, pool, redis):
    from aiogram.types import ChatPermissions

    admin_id = message.from_user.id
    if not await _is_admin(admin_id, bot, DAO_GROUP_ID):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Usage: `/unmute <user_id>`", parse_mode="Markdown")
        return

    target_id = int(args[1])
    full_perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await bot.restrict_chat_member(DAO_GROUP_ID, target_id, permissions=full_perms)
    except Exception as exc:
        await message.answer(f"⚠️ Could not unmute: {exc}")
        return

    # Clear verification lockout from Redis
    await redis.delete(f"verify:locked:{target_id}")
    await redis.delete(f"verify:attempts:{target_id}")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_muted = FALSE WHERE user_id = $1", target_id
        )

    try:
        await message.delete()
    except Exception:
        pass

    await bot.send_message(
        target_id,
        "✅ A moderator has manually approved your access. Welcome to the DAO!"
    )


# ===========================================================================
# PHASE 3 NEW: /trace — Glass-Break Protocol (Publicly Transparent)
# ===========================================================================
@router.message(
    Command("trace"),
    F.chat.id == DAO_GROUP_ID,
    F.reply_to_message,   # Must be a reply to an anonymous message
)
async def cmd_trace(message: Message, bot: Bot, pool, redis):
    """
    Public glass-break deanonymisation command.
    Usage (admin replies to an anon message): /trace <reason>
    """
    admin_id = message.from_user.id

    # --- Guard: real admin only ---
    if not await _is_admin(admin_id, bot, DAO_GROUP_ID):
        return

    target_msg = message.reply_to_message
    args = message.text.split(maxsplit=1)
    reason = args[1].strip() if len(args) > 1 else "No reason provided."

    # --- Step 1: Query anon_logs for the real author ---
    anon_msg_id = target_msg.message_id
    real_user_id: int | None = None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM anon_logs WHERE message_id = $1",
            anon_msg_id,
        )
        if row:
            real_user_id = row["user_id"]

    if not real_user_id:
        await message.reply(
            "⚠️ <b>Trace failed:</b> No anonymous log found for that message.\n"
            "The message may not have been sent via <code>/anon</code>, or the log has expired.",
            parse_mode="HTML",
        )
        return

    # --- Step 2: Fetch user details for the public report ---
    try:
        user_info = await bot.get_chat(real_user_id)
        username_str = f"@{user_info.username}" if user_info.username else "<i>(no username)</i>"
        full_name = user_info.full_name or "Unknown"
    except Exception:
        username_str = "<i>(could not fetch)</i>"
        full_name = "Unknown"

        # --- Step 3: Send the trace report to the PUBLIC chat ---
    trace_report = (
        f"🔍 <b>GLASS-BREAK — PUBLIC TRACE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Authorized by Admin:</b> {message.from_user.mention_html()}\n\n"
        f"<b>Name:</b> {full_name}\n"
        f"<b>Username:</b> {username_str}\n"
        f"<b>Trace Reason:</b> <i>{reason}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        await message.reply(trace_report, parse_mode="HTML")
    except Exception as exc:
        log.error("Could not send public trace report: %s", exc)

    try:
        await message.reply(trace_report, parse_mode="HTML")
    except Exception as exc:
        log.error("Could not send public trace report: %s", exc)

    # --- Step 4: Log the trace access in penalties for the backend audit trail ---
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO penalties (user_id, admin_id, action, reason, timestamp)
            VALUES ($1, $2, $3, $4, $5)
            """,
            real_user_id,
            admin_id,
            "warn",
            f"[GLASS-BREAK /trace] Admin `{admin_id}` deanonymised message `{anon_msg_id}` publicly. Reason: {reason}",
            datetime.now(tz=timezone.utc),
        )

# ---------------------------------------------------------------------------
# Internal helper: resolve the real user_id from a message
# (works for both regular messages and anonymous bot-forwarded ones)
# ---------------------------------------------------------------------------
async def _resolve_target(target_msg: Message, pool) -> int | None:
    """
    Returns the real user_id behind a message.
    - For normal messages: target_msg.from_user.id
    - For /anon bot-forwarded messages: queries anon_logs by message_id
    """
    # Regular message with a real sender
    if target_msg.from_user and not target_msg.from_user.is_bot:
        return target_msg.from_user.id

    # Could be an anon message forwarded by the bot — check logs
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM anon_logs WHERE message_id = $1",
            target_msg.message_id,
        )
        return row["user_id"] if row else None
