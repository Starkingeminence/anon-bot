"""
Acki Nacki DAO Community Bot
Phase 1-4 Core Infrastructure Framework

Stack  : Python 3.11+ | aiogram 3.x | asyncpg | redis-py | aiohttp
Hosting: Render Free Tier  → Webhook + $PORT binding + /health cron probe
DB     : Supabase Free Tier → Global asyncpg.Pool (never open/close per message)
Cache  : Upstash Redis      → All ephemeral state with strict TTL
"""

from __future__ import annotations

import logging
import os
import asyncio
from datetime import timezone
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# Configure before anything else so every module's logger inherits the format.
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# All config is read from environment variables. Never hard-code secrets.
# ─────────────────────────────────────────────────────────────────────────────
def _require(key: str) -> str:
    """Crash early with a readable error if a required env-var is missing."""
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Check your .env file or Render dashboard."
        )
    return value


BOT_TOKEN: str = _require("BOT_TOKEN")
WEBHOOK_HOST: str = _require("WEBHOOK_HOST").rstrip("/")
DATABASE_URL: str = _require("DATABASE_URL")
REDIS_URL: str = _require("REDIS_URL")

# Webhook path embeds the token as a secret path component — obscures the
# endpoint from non-Telegram callers without additional auth middleware.
WEBHOOK_PATH: str = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL: str = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

PORT: int = int(os.getenv("PORT", "8080"))
GROUP_ID: int = int(os.getenv("GROUP_ID", "0"))
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# Phase 4 Required Cryptographic & Network Variables (Strict Mode)
FOUNDER_WALLET: str = _require("FOUNDER_WALLET")
HOT_WALLET_ADDRESS: str = _require("HOT_WALLET_ADDRESS")
MULTISIG_WALLET_ADDRESS: str = _require("MULTISIG_WALLET_ADDRESS")
ACKI_NACKI_RPC: str = _require("ACKI_NACKI_RPC")

# Supabase free tier supports ~15 direct connections. We cap at 8 to leave
# headroom for migrations, Supabase Studio, and other tooling.
DB_POOL_MIN_SIZE: int = 1
DB_POOL_MAX_SIZE: int = 8


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE — asyncpg connection pool
# PRD §1: "The bot must never open/close connections per message."
# The pool is created once at startup and stored in app state; handlers
# acquire / release connections from it automatically via `async with`.
# ─────────────────────────────────────────────────────────────────────────────
async def create_db_pool() -> asyncpg.Pool:
    """
    Open a global asyncpg connection pool to Supabase.

    ssl="require"  → Supabase rejects non-TLS connections.
    max_inactive_connection_lifetime → prevents stale idle connections on
    Supabase's aggressively recycled free-tier DB.
    """
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        ssl="require",
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        max_queries=50_000,
        max_inactive_connection_lifetime=300.0,  # 5-minute idle recycle
        command_timeout=30.0,
    )
    logger.info(
        "✅ PostgreSQL pool created [min=%d, max=%d]",
        DB_POOL_MIN_SIZE,
        DB_POOL_MAX_SIZE,
    )
    return pool


async def verify_schema(pool: asyncpg.Pool) -> None:
    """
    Verify that all required tables exist. If they don't (fresh Supabase project
    before running schema.sql), raise immediately with an actionable message
    rather than a cryptic asyncpg error later.
    """
    required_tables = [
        "users",
        "penalties",
        "anon_logs",
        "verification_attempts",
        "referrals",
        "point_ledger",
        "spray_log",
        "wallets"
    ]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY($1::text[])
            """,
            required_tables,
        )
    found = {r["table_name"] for r in rows}
    missing = set(required_tables) - found
    if missing:
        raise RuntimeError(
            f"Database schema is incomplete. Missing tables: {missing}. "
            "Please apply Phase 4 SQL schema definitions in Supabase before starting the bot."
        )
    logger.info("✅ Database schema verified — all %d tables present", len(required_tables))


# ─────────────────────────────────────────────────────────────────────────────
# REDIS — Upstash async client
# PRD §1: "All ephemeral states must be stored in Redis with strict TTL."
# The same client is shared across all handlers via dispatcher workflow_data.
# ─────────────────────────────────────────────────────────────────────────────
async def create_redis_client() -> aioredis.Redis:
    """
    Connect to Upstash Redis.

    Upstash free tier uses TLS (rediss://) — the URL from the Upstash console
    already includes the correct scheme.
    decode_responses=True  → all keys/values are str, never raw bytes.
    """
    client: aioredis.Redis = aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    await client.ping()  # Fail-fast: surface bad credentials at startup
    logger.info("✅ Redis client connected (Upstash)")
    return client


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED TASKS
# PRD §3: Midnight Reset must execute at exactly 00:00 UTC.
# APScheduler runs inside the same asyncio event loop — no separate process.
# ─────────────────────────────────────────────────────────────────────────────
async def scheduled_daily_reset(pool: asyncpg.Pool) -> None:
    """
    00:00 UTC — reset points_daily for every user.
    Calls the database-side function defined in schema.sql to ensure the
    reset is atomic even if multiple bot instances were ever to run.
    PRD §3: Midnight Reset.
    """
    logger.info("⏰ Running scheduled daily points reset...")
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT reset_daily_points()")
        logger.info("✅ Daily points reset complete")
    except Exception:
        logger.exception("❌ Daily points reset FAILED — investigate immediately")


def build_scheduler(pool: asyncpg.Pool, redis: aioredis.Redis, bot: Bot) -> AsyncIOScheduler:
    """
    Wire up all recurring background engines into a single unified clock instance.
    Enforces distinct operational separations across jobs 1, 2, and 3.
    """
    from tasks.payout_engine import referral_bonus_batch, end_of_month_collation

    scheduler = AsyncIOScheduler(timezone="UTC")

    # JOB 1 (hourly) — referral_bonus_batch()
    # Computes 10% referral bonus for points earned in the past 60 minutes.
    # Reads from a `referrals` table, writes bonus increments to users.
    scheduler.add_job(
        referral_bonus_batch,
        trigger="interval",
        hours=1,
        args=[pool, redis, bot],
        id="referral_batch",
        replace_existing=True,
        misfire_grace_time=300,  # Grace room for free tier spin-ups
    )

    # JOB 2 (daily @ 00:00 UTC) — scheduled_daily_reset
    # Resets points_daily for all active system users natively inside PostgreSQL.
    scheduler.add_job(
        scheduled_daily_reset,
        trigger=CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
        args=[pool],
        id="daily_reset",
        replace_existing=True,
        misfire_grace_time=60,  # Allow up to 60s late start before skipping
    )

    # JOB 3 (monthly @ 00:00 UTC on 1st) — end_of_month_collation()
    # Full transactional payout sequence: Chat freeze, math checks, GraphQL payload dispatch, grand reset lifecycle.
    scheduler.add_job(
        end_of_month_collation,
        trigger=CronTrigger(day=1, hour=0, minute=0, timezone=timezone.utc),
        args=[pool, redis, bot],
        id="monthly_payout",
        replace_existing=True,
        misfire_grace_time=600,
    )

    return scheduler


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH ENDPOINT
# PRD §1: "A /health endpoint must be exposed for a 14-minute external cron
# ping to prevent cold starts."
#
# Set up a free cron at https://cron-job.org targeting:
#   GET https://your-service.onrender.com/health  every 14 minutes
# ─────────────────────────────────────────────────────────────────────────────
async def health_handler(request: web.Request) -> web.Response:
    """
    Liveness + readiness probe.
    Returns HTTP 200 {"status": "ok"} when DB and Redis are reachable.
    Returns HTTP 503 {"status": "degraded"} with per-service flags otherwise.
    Render's health check (if configured) can point here too.
    """
    pool: asyncpg.Pool = request.app["db_pool"]
    redis: aioredis.Redis = request.app["redis_client"]

    db_ok = False
    redis_ok = False

    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("Health check — DB probe failed: %s", exc)

    try:
        await redis.ping()
        redis_ok = True
    except Exception as exc:
        logger.warning("Health check — Redis probe failed: %s", exc)

    healthy = db_ok and redis_ok
    return web.json_response(
        {
            "status": "ok" if healthy else "degraded",
            "db": db_ok,
            "redis": redis_ok,
        },
        status=200 if healthy else 503,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER — aiogram 3.x Router tree
# Feature modules are registered here as they are built in subsequent phases.
# Workflow data (db_pool, redis, config) is injected into every handler via
# dispatcher.workflow_data — no global state accessed directly by handlers.
# ─────────────────────────────────────────────────────────────────────────────
def build_dispatcher(redis_url: str) -> Dispatcher:
    """
    Construct the root Dispatcher.
    """
    storage = RedisStorage.from_url(redis_url)
    dp = Dispatcher(storage=storage)

    # ── Phase 2-4: Active Routers ─────────────────────────────────────────────
    from routers.admin import router as admin_router
    from routers.economy import router as economy_router
    from routers.verification import router as verification_router
    from routers.treasury import router as treasury_router

    # ORDER MATTERS: Admin must be registered FIRST so it catches commands 
    # before the economy router catches standard chat messages.
    dp.include_router(admin_router)
    dp.include_router(verification_router)
    dp.include_router(treasury_router)      # ← Phase 4 User Read-Only Layer
    dp.include_router(economy_router)

    logger.info("✅ Dispatcher built with RedisStorage FSM & prioritizing routers")
    return dp


# ─────────────────────────────────────────────────────────────────────────────
# AIOHTTP LIFECYCLE HOOKS
# on_startup  → runs after aiohttp binds to $PORT, before accepting requests
# on_shutdown → runs when the process receives SIGTERM (Render graceful stop)
# ─────────────────────────────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    """
    Boot sequence:
    1. Open DB pool
    2. Connect Redis
    3. Verify schema
    4. Inject shared resources into dispatcher workflow_data
    5. Start APScheduler (Unified and compiled cleanly)
    6. Register Telegram webhook
    """
    bot: Bot = app["bot"]
    dp: Dispatcher = app["dp"]

    # ── 1. Database ───────────────────────────────────────────────────────────
    db_pool = await create_db_pool()
    app["db_pool"] = db_pool

    # ── 2. Redis ──────────────────────────────────────────────────────────────
    redis_client = await create_redis_client()
    app["redis_client"] = redis_client

    # ── 3. Schema check ───────────────────────────────────────────────────────
    await verify_schema(db_pool)

    # ── 4. Inject into Dispatcher workflow_data ───────────────────────────────
    # Fetch the bot's own ID so the admin router knows when it's being replied to
    me = await bot.get_me()

    # Handlers receive these as keyword arguments:
    dp.workflow_data.update(
        {
            "pool": db_pool,
            "redis": redis_client,
            "admin_ids": ADMIN_IDS,
            "group_id": GROUP_ID,
            "founder_wallet": FOUNDER_WALLET,
            "hot_wallet": HOT_WALLET_ADDRESS,
            "multisig_wallet": MULTISIG_WALLET_ADDRESS,
            "acki_nacki_rpc": ACKI_NACKI_RPC,
            "bot_id": me.id,
        }
    )

    # ── 5. APScheduler ────────────────────────────────────────────────────────
    # Instantiates the shared, single structural scheduler clock mapping jobs 1, 2, and 3
    scheduler = build_scheduler(db_pool, redis_client, bot)
    scheduler.start()
    app["scheduler"] = scheduler
    logger.info("✅ APScheduler started — Hourly bonus engine, 00:00 reset, and month-end payout active.")

    # ── 6. Telegram Webhook ───────────────────────────────────────────────────
    # drop_pending_updates=True: discard any updates queued during downtime
    # (Render cold-start gap). Prevents stale message flooding on restart.
    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
        secret_token=None,  # TODO: add WEBHOOK_SECRET env-var for production hardening
    )
    info = await bot.get_webhook_info()
    logger.info(
        "✅ Webhook set | url=%s | pending_update_count=%d | last_error=%s",
        info.url,
        info.pending_update_count,
        info.last_error_message or "none",
    )


async def on_shutdown(app: web.Application) -> None:
    """
    Graceful teardown — called by aiohttp on SIGTERM.
    """
    bot: Bot = app["bot"]

    # Stop APScheduler (waits for running jobs to finish, up to 5s)
    scheduler: AsyncIOScheduler = app.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("🔌 APScheduler stopped")

    # 🚨 CRITICAL FIX: We no longer delete the webhook here!
    # This prevents the old container from assassinating the webhook 
    # connection of the new container during zero-downtime deploys.
    logger.info("🔌 Webhook left intact for the active instance")

    # Close DB pool — drains all active connections cleanly
    db_pool: asyncpg.Pool = app.get("db_pool")
    if db_pool:
        await db_pool.close()
        logger.info("🔌 PostgreSQL pool closed")

    # Close Redis client
    redis_client: aioredis.Redis = app.get("redis_client")
    if redis_client:
        await redis_client.aclose()
        logger.info("🔌 Redis client closed")

    # Close the bot session
    await bot.session.close()
    logger.info("🔌 Bot session closed")


# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION FACTORY
# ─────────────────────────────────────────────────────────────────────────────
def create_app() -> web.Application:
    """
    Build and wire the aiohttp Application.

    Route map:
      GET  /health              → health_handler (cron keep-alive probe)
      POST /webhook/{BOT_TOKEN} → SimpleRequestHandler (Telegram updates)
    """
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(redis_url=REDIS_URL)

    app = web.Application()

    # Store bot and dispatcher on app so lifecycle hooks can reach them
    app["bot"] = bot
    app["dp"] = dp

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)

    # Routes
    app.router.add_get("/health", health_handler)

    # Register aiogram's webhook handler.
    # SimpleRequestHandler validates the Telegram-IP header and routes each
    # Update through the Dispatcher's middleware / router tree.
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)

    # setup_application wires aiogram's startup/shutdown signals into aiohttp's
    # lifecycle so aiogram middleware initialises correctly.
    setup_application(app, dp, bot=bot)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(
        "🚀 Starting Acki Nacki DAO Bot | port=%d | webhook=%s",
        PORT,
        WEBHOOK_URL,
    )
    app = create_app()
    web.run_app(
        app,
        host="0.0.0.0",   # Render requires 0.0.0.0 — never 127.0.0.1
        port=PORT,
        access_log=logger,
        # Graceful shutdown: give in-flight handlers 10s to complete
        shutdown_timeout=10,
    )
