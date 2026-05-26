"""
tasks/payout_engine.py
Phase 4 — Scheduled Task Engine

Three distinct jobs registered with APScheduler:

  Job 1 (hourly)   — referral_bonus_batch()
      Computes 10% referral bonus for points earned in the past 60 minutes.
      Reads from a `referrals` table, writes bonus increments to users.
      Designed to be idempotent via a Redis dedup key.

  Job 2 (daily @ 00:00 UTC) — midnight_daily_reset()
      Resets points_daily for all users (already spec'd in Phase 1/2;
      included here for single-file scheduler registration).

  Job 3 (monthly @ 00:00 UTC on 1st) — end_of_month_collation()
      Full transactional payout sequence:
        A. Freeze chat
        B. Run the percentages
        C. Dispatch GraphQL payload
        D. Grand reset lifecycle

All jobs accept pool, redis, and bot as injected dependencies — they are
bound at scheduler registration time in main.py (see snippets file).

Expected additional DB table (add to Supabase):

  referrals
    referrer_id   BIGINT  FK → users
    referee_id    BIGINT  FK → users  UNIQUE
    created_at    TIMESTAMP

  point_ledger   (hourly batch source)
    id            UUID PK default gen_random_uuid()
    user_id       BIGINT  FK → users
    points        FLOAT
    earned_at     TIMESTAMP default now()
    ref_processed BOOLEAN default FALSE
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DAO_GROUP_ID: int = int(os.environ.get("DAO_GROUP_ID", "-1001234567890"))
ACKI_NACKI_RPC_URL: str = os.environ.get("ACKI_NACKI_RPC_URL", "")
MULTISIG_WALLET: str = os.environ.get("MULTISIG_WALLET_ADDRESS", "")
FOUNDER_WALLET: str = os.environ.get("FOUNDER_WALLET_ADDRESS", "")
GRAPHQL_ENDPOINT: str = os.environ.get("ACKI_NACKI_GRAPHQL_ENDPOINT", "")
GRAPHQL_API_KEY: str = os.environ.get("ACKI_NACKI_API_KEY", "")

# Admin IDs — must match admin.py. Loaded from env for DRY principle.
ADMIN_IDS: list[int] = [
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

REFERRAL_BONUS_RATE: float = 0.10          # 10%
FOUNDER_SHARE: float = 0.01               # 1%
VELOCITY_TAX: float = 0.05               # 5% Aura Fee on /spray etc.
TOP_N_WINNERS: int = 10

# Redis dedup key for referral batch — prevents double-processing on crash/retry
def _rkey_ref_batch_lock(hour_ts: str) -> str:
    return f"batch:ref:{hour_ts}"


# ===========================================================================
# JOB 1 — Hourly Referral Bonus Batch
# ===========================================================================
async def referral_bonus_batch(pool: asyncpg.Pool, redis, bot) -> None:
    """
    Runs every 60 minutes.

    1. Computes the window: (now - 1h, now).
    2. Queries point_ledger for unprocessed rows in that window.
    3. Joins with referrals table to find referrers.
    4. Bulk-updates referrers' point balances.
    5. Marks processed rows ref_processed = TRUE.

    Uses a Redis lock keyed to the hour timestamp to guarantee
    exactly-once execution even if the scheduler fires a duplicate task.
    """
    now_utc = datetime.now(tz=timezone.utc)
    window_start = now_utc - timedelta(hours=1)
    hour_key = now_utc.strftime("%Y%m%d%H")
    lock_key = _rkey_ref_batch_lock(hour_key)

    # Acquire distributed lock (10-minute TTL — job should finish well within that)
    acquired = await redis.set(lock_key, "1", nx=True, ex=600)
    if not acquired:
        log.info("Referral batch %s already running — skipping.", hour_key)
        return

    log.info("Referral batch starting for window %s → %s", window_start, now_utc)

    try:
        async with pool.acquire() as conn:
            # Fetch all unprocessed ledger rows in the window, joined to referrals
            rows = await conn.fetch(
                """
                SELECT
                    pl.id        AS ledger_id,
                    pl.user_id   AS referee_id,
                    pl.points    AS earned_points,
                    r.referrer_id
                FROM point_ledger pl
                JOIN referrals r ON r.referee_id = pl.user_id
                WHERE pl.earned_at >= $1
                  AND pl.earned_at <  $2
                  AND pl.ref_processed = FALSE
                """,
                window_start,
                now_utc,
            )

            if not rows:
                log.info("Referral batch: no unprocessed rows in window.")
                return

            # Aggregate bonus per referrer
            referrer_bonus: dict[int, float] = {}
            ledger_ids: list[uuid.UUID] = []
            for row in rows:
                bonus = row["earned_points"] * REFERRAL_BONUS_RATE
                referrer_bonus[row["referrer_id"]] = (
                    referrer_bonus.get(row["referrer_id"], 0.0) + bonus
                )
                ledger_ids.append(row["ledger_id"])

            log.info(
                "Referral batch: %d referrers receive bonuses from %d ledger rows.",
                len(referrer_bonus), len(ledger_ids),
            )

            # Bulk upsert referrer bonuses
            async with conn.transaction():
                for referrer_id, bonus_pts in referrer_bonus.items():
                    await conn.execute(
                        """
                        UPDATE users
                        SET
                            points_daily    = LEAST(points_daily + $1, 700),
                            points_monthly  = points_monthly + $1,
                            points_lifetime = points_lifetime + $1
                        WHERE user_id = $2
                        """,
                        bonus_pts,
                        referrer_id,
                    )

                # Mark rows as processed — atomic with the balance update
                await conn.execute(
                    "UPDATE point_ledger SET ref_processed = TRUE WHERE id = ANY($1::uuid[])",
                    ledger_ids,
                )

        log.info("Referral batch complete. %d referrers updated.", len(referrer_bonus))

    except Exception as exc:
        log.exception("Referral batch failed: %s", exc)
        # Release lock on failure so the next scheduler tick can retry
        await redis.delete(lock_key)
        raise


# ===========================================================================
# JOB 2 — Daily Midnight Reset (points_daily → 0)
# ===========================================================================
async def midnight_daily_reset(pool: asyncpg.Pool, redis, bot) -> None:
    """Resets points_daily for all users at 00:00 UTC."""
    log.info("Daily reset: resetting points_daily.")
    try:
        async with pool.acquire() as conn:
            result = await conn.execute("UPDATE users SET points_daily = 0")
        log.info("Daily reset complete: %s", result)
    except Exception as exc:
        log.exception("Daily reset failed: %s", exc)
        raise


# ===========================================================================
# JOB 3 — End-of-Month Collation (The Group Lock)
# ===========================================================================
async def end_of_month_collation(pool: asyncpg.Pool, redis, bot) -> None:
    """
    Transactional end-of-month payout sequence.
    Triggered at 00:00 UTC on the 1st of each month.

    Steps: A → Freeze, B → Percentages, C → GraphQL dispatch, D → Grand Reset.
    """
    log.info("END-OF-MONTH COLLATION STARTING.")

    # ── Step A: Chat Freeze ──────────────────────────────────────────────────
    await _step_a_freeze_chat(bot)

    # ── Step B: Run the percentages ──────────────────────────────────────────
    payout_map, monthly_pool, total_points = await _step_b_calculate_payouts(pool, bot)

    if not payout_map:
        log.warning("Payout map is empty — aborting collation.")
        await _restore_chat(bot, pool)
        return

    # ── Step C: Dispatch GraphQL payload ─────────────────────────────────────
    success = await _step_c_dispatch_graphql(payout_map, monthly_pool, bot)

    if not success:
        # Do NOT reset data if blockchain dispatch failed — preserve for retry
        await bot.send_message(
            DAO_GROUP_ID,
            "⚠️ *Payout dispatch encountered an error.*\n"
            "The accounting data has been preserved. Admins have been notified.\n"
            "Chat will be restored while the team investigates.",
            parse_mode="Markdown",
        )
        await _restore_chat(bot, pool)
        return

    # ── Step D: Grand Reset Lifecycle ────────────────────────────────────────
    await _step_d_grand_reset(pool, bot, payout_map)

    log.info("END-OF-MONTH COLLATION COMPLETE.")


# ---------------------------------------------------------------------------
# Step A — Freeze the chat
# ---------------------------------------------------------------------------
async def _step_a_freeze_chat(bot) -> None:
    from aiogram.types import ChatPermissions

    frozen_perms = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    try:
        await bot.set_chat_permissions(DAO_GROUP_ID, frozen_perms)
        log.info("Step A: chat frozen.")
    except Exception as exc:
        log.error("Step A: failed to freeze chat: %s", exc)

    await bot.send_message(
        DAO_GROUP_ID,
        "🔒 *The campaign month has ended.*\n\n"
        "The chat is temporarily frozen for accounting collation…\n\n"
        "💼 We are computing your NACKL allocations now.\n"
        "This typically takes 2–3 minutes. Stand by.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Step B — Compute allocations
# ---------------------------------------------------------------------------
async def _step_b_calculate_payouts(
    pool: asyncpg.Pool,
    bot,
) -> tuple[dict[int, dict], float, float]:
    """
    Returns:
        payout_map  — {user_id: {wallet, points, nackl_allocation}}
        monthly_pool — the 1/12th community pool (after founder cut)
        total_points — sum of all monthly points (users + ghost admin points)
    """
    log.info("Step B: computing allocations.")

    async with aiohttp.ClientSession() as session:
        from routers.treasury import fetch_multisig_balance
        balance = await fetch_multisig_balance(session)

    if balance is None:
        log.error("Step B: could not fetch treasury balance.")
        await bot.send_message(
            DAO_GROUP_ID,
            "⚠️ RPC unreachable during payout. Admins notified.",
        )
        return {}, 0.0, 0.0

    one_twelfth = balance / 12.0
    founder_cut = one_twelfth * FOUNDER_SHARE
    community_pool = one_twelfth * (1.0 - FOUNDER_SHARE)

    log.info(
        "Step B: treasury=%.4f, 1/12th=%.4f, community_pool=%.4f",
        balance, one_twelfth, community_pool,
    )

    async with pool.acquire() as conn:
        # All real users and their monthly points
        user_rows = await conn.fetch(
            "SELECT user_id, points_monthly FROM users WHERE points_monthly > 0"
        )

        # Ghost Points: admins receive the same as the #1 earner
        top_row = await conn.fetchrow(
            "SELECT points_monthly FROM users ORDER BY points_monthly DESC LIMIT 1"
        )
        ghost_points = top_row["points_monthly"] if top_row else 0.0

        # User wallets — assumes a `wallets` table: user_id BIGINT, address TEXT
        wallet_rows = await conn.fetch("SELECT user_id, address FROM wallets")

    wallet_map: dict[int, str] = {r["user_id"]: r["address"] for r in wallet_rows}

    # Build combined point pool (users + 4 ghost admin entries)
    combined: dict[int, float] = {r["user_id"]: float(r["points_monthly"]) for r in user_rows}
    for admin_id in ADMIN_IDS:
        combined[admin_id] = combined.get(admin_id, 0.0) + ghost_points

    total_points = sum(combined.values())
    if total_points == 0:
        log.warning("Step B: total_points = 0, nothing to distribute.")
        return {}, community_pool, 0.0

    payout_map: dict[int, dict] = {}
    for uid, pts in combined.items():
        share = pts / total_points
        allocation = community_pool * share
        payout_map[uid] = {
            "wallet": wallet_map.get(uid, ""),
            "points": pts,
            "share_pct": round(share * 100, 6),
            "nackl_allocation": round(allocation, 9),
        }

    log.info("Step B: %d recipients computed. Total points: %.2f", len(payout_map), total_points)
    return payout_map, community_pool, total_points


# ---------------------------------------------------------------------------
# Step C — Dispatch GraphQL payload
# ---------------------------------------------------------------------------
async def _step_c_dispatch_graphql(
    payout_map: dict[int, dict],
    monthly_pool: float,
    bot,
) -> bool:
    """
    Constructs the immutable distribution payload and dispatches to
    the Acki Nacki GraphQL endpoint.
    Returns True on 200 OK, False on any failure.
    """
    log.info("Step C: constructing GraphQL payload.")

    recipients = [
        {
            "wallet_address": data["wallet"],
            "nackl_amount": str(data["nackl_allocation"]),  # String for precision safety
            "share_pct": data["share_pct"],
        }
        for uid, data in payout_map.items()
        if data.get("wallet")  # Only include users with registered wallets
    ]

    # Founder allocation as a separate top-level entry
    founder_entry = {
        "wallet_address": FOUNDER_WALLET,
        "nackl_amount": str(round(monthly_pool * FOUNDER_SHARE / (1.0 - FOUNDER_SHARE), 9)),
        "share_pct": 1.0,
        "label": "founder_reserve",
    }

    mutation = """
    mutation DistributeMonthlyRewards($payload: DistributionPayload!) {
      distributeRewards(payload: $payload) {
        success
        transaction_id
        message
      }
    }
    """

    variables = {
        "payload": {
            "campaign_month": datetime.now(tz=timezone.utc).strftime("%Y-%m"),
            "total_pool_nackl": str(round(monthly_pool, 9)),
            "founder": founder_entry,
            "recipients": recipients,
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        }
    }

    log.info("Step C: dispatching %d recipients to GraphQL.", len(recipients))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GRAPHQL_ENDPOINT,
                json={"query": mutation, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GRAPHQL_API_KEY}",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.error("Step C: GraphQL non-200 response: %s", resp.status)
                    body = await resp.text()
                    log.error("Step C: response body: %s", body[:500])
                    return False

                data = await resp.json()
                result = (
                    data.get("data", {})
                        .get("distributeRewards", {})
                )
                if not result.get("success"):
                    log.error("Step C: distributeRewards returned success=false: %s", result)
                    return False

                tx_id = result.get("transaction_id", "N/A")
                log.info("Step C: dispatch successful. TX ID: %s", tx_id)
                return True

    except asyncio.TimeoutError:
        log.error("Step C: GraphQL request timed out.")
        return False
    except Exception as exc:
        log.exception("Step C: unexpected error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Step D — Grand Reset Lifecycle
# ---------------------------------------------------------------------------
async def _step_d_grand_reset(
    pool: asyncpg.Pool,
    bot,
    payout_map: dict[int, dict],
) -> None:
    """
    Post-confirmation cleanup:
    1. Reset points_monthly → 0 for all users.
    2. Strip old Top-10 pseudo-admin titles.
    3. Compute & grant new Top-10 titles (Chatters, Philanthropists, Inviters).
    4. Restore chat permissions.
    5. Announce new season with fresh leaderboard.
    """
    log.info("Step D: grand reset starting.")

    async with pool.acquire() as conn:

        # ── D1: Reset monthly points ─────────────────────────────────────────
        await conn.execute("UPDATE users SET points_monthly = 0")
        log.info("Step D: points_monthly reset to 0.")

        # ── D2: Identify & strip previous Top-10 titles ──────────────────────
        prev_top10_rows = await conn.fetch(
            """
            SELECT user_id FROM users
            WHERE monthly_title IS NOT NULL
            """
        )
        prev_top10_ids = [r["user_id"] for r in prev_top10_rows]

        for uid in prev_top10_ids:
            try:
                # Demote custom title (pass empty string to clear)
                await bot.promote_chat_member(
                    DAO_GROUP_ID,
                    uid,
                    can_change_info=False,
                    can_delete_messages=False,
                    can_invite_users=False,
                    can_restrict_members=False,
                    can_pin_messages=False,
                    can_promote_members=False,
                )
                await bot.set_chat_administrator_custom_title(
                    DAO_GROUP_ID, uid, ""
                )
            except Exception as exc:
                log.warning("Step D: could not strip title from %d: %s", uid, exc)

        await conn.execute(
            "UPDATE users SET monthly_title = NULL WHERE monthly_title IS NOT NULL"
        )

        # ── D3: Compute new Top-10 by category ───────────────────────────────
        # Top Chatters — highest lifetime points this month (already reset above,
        # so we use payout_map as the source of truth for this cycle)
        sorted_by_points = sorted(
            payout_map.items(), key=lambda x: x[1]["points"], reverse=True
        )
        top_chatters = sorted_by_points[:TOP_N_WINNERS]

        # Top Philanthropists — most NACKL distributed via /spray (spray_log table)
        top_philanthropists = await conn.fetch(
            """
            SELECT user_id, SUM(amount) AS total_sprayed
            FROM spray_log
            WHERE sprayed_at >= date_trunc('month', now() - interval '1 month')
              AND sprayed_at <  date_trunc('month', now())
            GROUP BY user_id
            ORDER BY total_sprayed DESC
            LIMIT $1
            """,
            TOP_N_WINNERS,
        )

        # Top Inviters — most successful referrals this month
        top_inviters = await conn.fetch(
            """
            SELECT referrer_id AS user_id, COUNT(*) AS invite_count
            FROM referrals
            WHERE created_at >= date_trunc('month', now() - interval '1 month')
              AND created_at <  date_trunc('month', now())
            GROUP BY referrer_id
            ORDER BY invite_count DESC
            LIMIT $1
            """,
            TOP_N_WINNERS,
        )

        # ── D4: Grant new pseudo-admin titles ────────────────────────────────
        title_assignments: dict[int, str] = {}

        for rank, (uid, _) in enumerate(top_chatters, start=1):
            title_assignments[uid] = f"🏆 Top Chatter #{rank}"

        for rank, row in enumerate(top_philanthropists, start=1):
            uid = row["user_id"]
            if uid not in title_assignments:  # Don't overwrite Chatter title
                title_assignments[uid] = f"💸 Philanthropist #{rank}"

        for rank, row in enumerate(top_inviters, start=1):
            uid = row["user_id"]
            if uid not in title_assignments:
                title_assignments[uid] = f"🤝 Top Inviter #{rank}"

        for uid, title in title_assignments.items():
            try:
                # Promote with all moderation switches OFF — cosmetic only
                await bot.promote_chat_member(
                    DAO_GROUP_ID,
                    uid,
                    can_change_info=False,
                    can_delete_messages=False,
                    can_invite_users=True,    # Minimal permission to allow custom title
                    can_restrict_members=False,
                    can_pin_messages=False,
                    can_promote_members=False,
                    is_anonymous=False,
                )
                await bot.set_chat_administrator_custom_title(DAO_GROUP_ID, uid, title)
            except Exception as exc:
                log.warning("Step D: could not grant title '%s' to %d: %s", title, uid, exc)

        # Persist titles in DB
        for uid, title in title_assignments.items():
            await conn.execute(
                "UPDATE users SET monthly_title = $1 WHERE user_id = $2",
                title, uid,
            )

    # ── D5: Restore chat permissions ─────────────────────────────────────────
    await _restore_chat(bot, pool)

    # ── D6: Announce new season ───────────────────────────────────────────────
    await _broadcast_new_season(bot, top_chatters, top_philanthropists, top_inviters)

    log.info("Step D: grand reset complete.")


# ---------------------------------------------------------------------------
# Helper: restore chat permissions
# ---------------------------------------------------------------------------
async def _restore_chat(bot, pool) -> None:
    from aiogram.types import ChatPermissions

    open_perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await bot.set_chat_permissions(DAO_GROUP_ID, open_perms)
        log.info("Chat permissions restored.")
    except Exception as exc:
        log.error("Failed to restore chat: %s", exc)


# ---------------------------------------------------------------------------
# Helper: stream the new-season announcement
# ---------------------------------------------------------------------------
async def _broadcast_new_season(
    bot,
    top_chatters: list,
    top_philanthropists,
    top_inviters,
) -> None:
    """
    Streams the new leaderboard announcement using progressive message edits
    to match the Telegram Streaming Text UX pattern from the spec.
    """
    month_label = datetime.now(tz=timezone.utc).strftime("%B %Y")

    # Initial message
    base = (
        "🎉 *Accounting complete! Welcome to the new campaign season.*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 *{month_label}* — Season now open!\n\n"
        "💬 Tokens have been dispatched to all registered wallets.\n"
        "Use `/treasury` to see the new runway.\n\n"
        "🏆 *New Monthly Title Holders*\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    sent = await bot.send_message(DAO_GROUP_ID, base, parse_mode="Markdown")

    # Stream Top Chatters
    chatter_lines = [base, "\n*🗣️ Top Chatters:*"]
    for rank, (uid, data) in enumerate(top_chatters[:5], start=1):
        chatter_lines.append(f"  `#{rank}` — `{uid}` · {data['points']:.0f} pts")
        await sent.edit_text("\n".join(chatter_lines), parse_mode="Markdown")
        await asyncio.sleep(0.4)   # Pacing for streaming effect

    # Stream Top Philanthropists
    chatter_lines.append("\n*💸 Top Philanthropists:*")
    for rank, row in enumerate(list(top_philanthropists)[:5], start=1):
        chatter_lines.append(f"  `#{rank}` — `{row['user_id']}` · {row['total_sprayed']:.2f} NACKL sprayed")
        await sent.edit_text("\n".join(chatter_lines), parse_mode="Markdown")
        await asyncio.sleep(0.4)

    # Stream Top Inviters
    chatter_lines.append("\n*🤝 Top Inviters:*")
    for rank, row in enumerate(list(top_inviters)[:5], start=1):
        chatter_lines.append(f"  `#{rank}` — `{row['user_id']}` · {row['invite_count']} referrals")
        await sent.edit_text("\n".join(chatter_lines), parse_mode="Markdown")
        await asyncio.sleep(0.4)

    # Footer
    chatter_lines.append("\n━━━━━━━━━━━━━━━━━━━━━")
    chatter_lines.append("🐝 *Start chatting and replying to climb this month's leaderboard!*")
    await sent.edit_text("\n".join(chatter_lines), parse_mode="Markdown")
