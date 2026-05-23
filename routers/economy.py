"""
routers/economy.py — Chat Economy
Phase 2 | PRD §3

Responsibilities:
  • Calculate point delta for every message type
  • Enforce The Spark Mechanic (reply-only point award)
  • Call award_points() Postgres function (handles 700-pt cap atomically)
  • Stream the /leaderboard via progressive message edits
  • Expose a /score command for personal daily/monthly/lifetime totals
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

# ── Router setup ──────────────────────────────────────────────────────────────
# All handlers in this router are scoped to group/supergroup chats ONLY.
# The /leaderboard and /score commands also fire in groups, so this is correct.
router = Router(name="economy")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# ── Constants ─────────────────────────────────────────────────────────────────
DAILY_CAP: int = 700          # PRD §3: The Daily Peg
LEADERBOARD_TOP_N: int = 10   # How many users to show
STREAM_DELAY: float = 0.07    # Seconds between each streamed leaderboard line
MEDALS: tuple[str, ...] = ("🥇", "🥈", "🥉")


# =============================================================================
# POINT CALCULATION
# =============================================================================

def _calculate_delta(message: Message) -> float:
    """
    Map a Telegram message onto its point value.
    PRD §3 — Point Valuation (exact rules, no deviations):

      Text > 15 chars                              → 1.0
      Text ≤ 15 chars                              → 0.5
      Voice note > 15 seconds                      → 1.0
      Voice note ≤ 15 seconds                      → 0.5
      Photo / Document / Video + caption > 15 chars → 1.0
      Photo / Document / Video + caption ≤ 15 chars → 0.5
      Sticker / GIF / Video note / Audio            → 0.5
      Poll                                          → 0.0
    """
    # Polls explicitly earn zero — must be checked first to avoid falling through
    if message.poll:
        return 0.0

    # Text messages (commands are excluded by the router-level filter below)
    if message.text:
        return 1.0 if len(message.text) > 15 else 0.5

    # Voice notes: duration attribute is in whole seconds
    if message.voice:
        return 1.0 if message.voice.duration > 15 else 0.5

    # Media with optional captions — photo, document, video
    if message.photo or message.document or message.video:
        caption = message.caption or ""
        return 1.0 if len(caption) > 15 else 0.5

    # Stickers, animated GIFs, round video notes, audio files — always 0.5
    if message.sticker or message.animation or message.video_note or message.audio:
        return 0.5

    # Catch-all for any future Telegram message type we haven't explicitly handled
    return 0.5


# =============================================================================
# DATABASE HELPERS
# =============================================================================

async def _award_to_user(
    pool: asyncpg.Pool,
    user_id: int,
    group_id: int,
    delta: float,
) -> float:
    """
    Invoke the award_points(user_id, group_id, delta) PostgreSQL function
    defined in schema.sql. The function:
      • Upserts the user row if they don't exist yet
      • Acquires a FOR UPDATE row lock to prevent race conditions
      • Enforces the 700-point daily cap
      • Increments points_daily, points_monthly, points_lifetime atomically
      • Returns the *actual* delta awarded (0 if cap was already reached)

    This is the ONLY place in the codebase that touches point columns.
    Never issue a raw UPDATE on points — always go through this function.
    """
    awarded: Optional[float] = await pool.fetchval(
        "SELECT award_points($1::BIGINT, $2::BIGINT, $3::FLOAT)",
        user_id,
        group_id,
        delta,
    )
    return awarded if awarded is not None else 0.0


async def _upsert_user(
    pool: asyncpg.Pool,
    user_id: int,
    group_id: int,
    language_code: Optional[str],
) -> None:
    """
    Ensure the user row exists with current metadata.
    Called on the SENDER only — the recipient is upserted inside award_points().
    ON CONFLICT DO UPDATE refreshes language_code so the moderation DM
    translation (PRD §7) always has the latest value.
    """
    await pool.execute(
        """
        INSERT INTO users (user_id, group_id, language_code)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE
            SET language_code = EXCLUDED.language_code,
                group_id      = EXCLUDED.group_id
        """,
        user_id,
        group_id,
        language_code,
    )


# =============================================================================
# SPARK MECHANIC — core message handler
# =============================================================================

@router.message(
    # Exclude all bot commands — those are handled by their own routers
    ~F.text.startswith("/"),
    # Never process messages from other bots
    F.from_user.is_bot.is_(False),
)
async def handle_group_message(
    message: Message,
    db_pool: asyncpg.Pool,
) -> None:
    """
    PRD §3 — The Spark Mechanic:

    "Points are only awarded if the message is a direct reply to another user.
    Both the sender and the person being replied to receive the points."

    If there is no reply_to_message, this handler returns immediately and
    silently — no points, no feedback to the user.

    Both awards are issued concurrently (asyncio.gather) to minimise DB
    round-trip latency under high-concurrency group traffic.
    """
    # ── Guard 1: Must be a reply ──────────────────────────────────────────────
    if not message.reply_to_message:
        return

    # ── Guard 2: Replied-to message must have a real human author ─────────────
    replied_to_user = message.reply_to_message.from_user
    if not replied_to_user or replied_to_user.is_bot:
        return   # Don't reward replies to bots or anonymous group admins

    sender_id = message.from_user.id
    recipient_id = replied_to_user.id
    chat_id = message.chat.id

    # ── Guard 3: Prevent self-reply point farming ─────────────────────────────
    if sender_id == recipient_id:
        return

    # ── Calculate delta (0.0 = no DB call needed) ────────────────────────────
    delta = _calculate_delta(message)
    if delta == 0.0:
        return  # Polls — don't touch the DB at all

    # ── Sync user metadata (language_code for future moderation DMs) ──────────
    # Fire-and-forget: we don't await this before awarding points so the
    # latency-sensitive award_points calls can run immediately.
    asyncio.create_task(
        _upsert_user(
            db_pool,
            sender_id,
            chat_id,
            message.from_user.language_code,
        )
    )

    # ── Award both participants concurrently ──────────────────────────────────
    # award_points() handles the upsert for the recipient internally,
    # so we don't need a separate upsert call for them.
    sender_awarded, recipient_awarded = await asyncio.gather(
        _award_to_user(db_pool, sender_id, chat_id, delta),
        _award_to_user(db_pool, recipient_id, chat_id, delta),
        return_exceptions=True,   # Don't crash the handler if one award fails
    )

    # Log exceptions without crashing — a failed award is non-fatal
    if isinstance(sender_awarded, Exception):
        logger.error(
            "award_points failed for sender %d: %s", sender_id, sender_awarded
        )
    if isinstance(recipient_awarded, Exception):
        logger.error(
            "award_points failed for recipient %d: %s", recipient_id, recipient_awarded
        )

    logger.debug(
        "Spark | chat=%d | sender=%d(+%.1f) | recipient=%d(+%.1f) | "
        "msg_type=%s | delta=%.1f",
        chat_id,
        sender_id,
        sender_awarded if isinstance(sender_awarded, float) else 0,
        recipient_id,
        recipient_awarded if isinstance(recipient_awarded, float) else 0,
        _message_type_label(message),
        delta,
    )


def _message_type_label(message: Message) -> str:
    """Return a human-readable label for debug logging."""
    if message.text:
        return "text"
    if message.voice:
        return "voice"
    if message.photo:
        return "photo"
    if message.document:
        return "document"
    if message.video:
        return "video"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "gif"
    if message.audio:
        return "audio"
    if message.video_note:
        return "video_note"
    if message.poll:
        return "poll"
    return "unknown"


# =============================================================================
# /leaderboard — streaming monthly top-10
# =============================================================================

@router.message(Command("leaderboard"))
async def cmd_leaderboard(
    message: Message,
    bot: Bot,
    db_pool: asyncpg.Pool,
) -> None:
    """
    PRD §1 — Streaming Text UX:
    "The bot must utilize the Telegram Streaming Text feature for leaderboards."

    Implementation: send a placeholder message, then progressively edit it
    with each rank line (80ms delay between edits) to simulate a real-time
    typing-out effect. This avoids rate-limit issues on rapid edit bursts
    while still providing the streaming UX the PRD specifies.

    Queries v_monthly_leaderboard (defined in schema.sql).
    """
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Placeholder message — user sees this while DB query runs
    placeholder = await message.answer("📊 <b>Loading leaderboard…</b>")

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id,
                       points_monthly,
                       points_lifetime,
                       rank
                  FROM v_monthly_leaderboard
                 WHERE group_id = $1
                 ORDER BY rank
                 LIMIT $2
                """,
                message.chat.id,
                LEADERBOARD_TOP_N,
            )
    except Exception as exc:
        logger.exception("Leaderboard DB query failed: %s", exc)
        await placeholder.edit_text("❌ Failed to load leaderboard. Please try again.")
        return

    if not rows:
        await placeholder.edit_text(
            "📊 <b>Monthly Leaderboard</b>\n\n"
            "No points have been earned yet this month.\n\n"
            "💡 Reply to other users' messages to earn points! 🐝"
        )
        return

    # ── Stream the leaderboard line by line ───────────────────────────────────
    header = (
        "🏆 <b>Monthly Leaderboard</b>\n"
        "<i>Top earners · resets on the 1st</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    accumulated = header

    for i, row in enumerate(rows):
        # Medal for top-3, rank number for the rest
        medal = MEDALS[i] if i < 3 else f"  #{i + 1}"

        # tg://user?id=X renders as a tappable mention in all Telegram clients
        # even if the bot has never interacted with the user directly.
        mention = f'<a href="tg://user?id={row["user_id"]}">User</a>'

        # Monthly points (primary sort) + lifetime total (secondary context)
        line = (
            f"{medal}  {mention}"
            f" — <b>{int(row['points_monthly']):,}</b> pts"
            f" <i>({int(row['points_lifetime']):,} lifetime)</i>\n"
        )
        accumulated += line

        # Edit the placeholder with the accumulated text so far
        try:
            await placeholder.edit_text(accumulated)
        except Exception:
            # "Message is not modified" — safe to ignore, keep streaming
            pass

        await asyncio.sleep(STREAM_DELAY)

    # ── Final footer ──────────────────────────────────────────────────────────
    footer = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Reply to messages to earn points. "
        f"Max {DAILY_CAP} pts/day (Bee Engine peg).</i>"
    )
    try:
        await placeholder.edit_text(accumulated + footer)
    except Exception:
        pass


# =============================================================================
# /score — personal stats snapshot
# =============================================================================

@router.message(Command("score"))
async def cmd_score(
    message: Message,
    db_pool: asyncpg.Pool,
) -> None:
    """
    Shows the requesting user their own daily / monthly / lifetime points.
    Quick self-check — doesn't reveal other users' data.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT points_daily,
                   points_monthly,
                   points_lifetime,
                   warning_count,
                   COALESCE(rank, 0) AS rank
              FROM users u
              LEFT JOIN LATERAL (
                  SELECT rank
                    FROM v_monthly_leaderboard
                   WHERE user_id = $1
                     AND group_id = $2
              ) lb ON TRUE
             WHERE u.user_id = $1
            """,
            user_id,
            chat_id,
        )

    if not row:
        await message.reply(
            "👤 You haven't earned any points yet.\n\n"
            "💡 Reply to another user's message to start earning! 🐝"
        )
        return

    daily_remaining = max(0, DAILY_CAP - row["points_daily"])
    rank_text = f"#{int(row['rank'])}" if row["rank"] else "Unranked"

    await message.reply(
        f"📈 <b>Your Stats</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📅 Today:     <b>{int(row['points_daily']):,}</b> pts "
        f"<i>({daily_remaining} remaining today)</i>\n"
        f"📆 This month: <b>{int(row['points_monthly']):,}</b> pts\n"
        f"🏆 Lifetime:   <b>{int(row['points_lifetime']):,}</b> pts\n"
        f"📊 Rank:       <b>{rank_text}</b>\n"
        f"⚠️ Warnings:  <b>{row['warning_count']}</b>"
    )
