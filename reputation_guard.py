from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

BAN_THRESHOLD = 3  # configurable later


# -----------------------------
# DATABASE SETUP
# -----------------------------

async def init_reputation_tables():
    """
    Call once on startup.
    """
    conn = await get_db_connection()
    if not conn:
        return

    await conn.execute("""
    CREATE TABLE IF NOT EXISTS cross_group_bans (
        user_id     BIGINT,
        group_id    BIGINT,
        banned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        active      BOOLEAN DEFAULT TRUE,
        PRIMARY KEY (user_id, group_id)
    );
    """)

    await conn.execute("""
    CREATE TABLE IF NOT EXISTS join_overrides (
        user_id     BIGINT,
        group_id    BIGINT,
        admin_id    BIGINT,
        reason      TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, group_id)
    );
    """)

    await conn.close()


# -----------------------------
# BAN TRACKING
# -----------------------------

async def record_ban(user_id: int, group_id: int):
    """
    Call this whenever a user is banned in a group.
    """
    conn = await get_db_connection()
    if not conn:
        return

    await conn.execute("""
        INSERT INTO cross_group_bans (user_id, group_id, active)
        VALUES ($1, $2, TRUE)
        ON CONFLICT (user_id, group_id)
        DO UPDATE SET active = TRUE, banned_at = CURRENT_TIMESTAMP
    """, user_id, group_id)

    await conn.close()


async def lift_ban(user_id: int, group_id: int):
    """
    Call this whenever a user is unbanned.
    """
    conn = await get_db_connection()
    if not conn:
        return

    await conn.execute("""
        UPDATE cross_group_bans
        SET active = FALSE
        WHERE user_id = $1 AND group_id = $2
    """, user_id, group_id)

    await conn.close()


# -----------------------------
# BAN COUNT
# -----------------------------

async def active_ban_count(user_id: int) -> int:
    conn = await get_db_connection()
    if not conn:
        return 0

    row = await conn.fetchrow("""
        SELECT COUNT(*) AS count
        FROM cross_group_bans
        WHERE user_id = $1 AND active = TRUE
    """, user_id)

    await conn.close()
    return row["count"] if row else 0


# -----------------------------
# OVERRIDE CHECK
# -----------------------------

async def has_override(user_id: int, group_id: int) -> bool:
    conn = await get_db_connection()
    if not conn:
        return False

    row = await conn.fetchrow("""
        SELECT 1 FROM join_overrides
        WHERE user_id = $1 AND group_id = $2
    """, user_id, group_id)

    await conn.close()
    return row is not None


async def create_override(user_id: int, group_id: int, admin_id: int, reason: str = None):
    conn = await get_db_connection()
    if not conn:
        return

    await conn.execute("""
        INSERT INTO join_overrides (user_id, group_id, admin_id, reason)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, group_id) DO NOTHING
    """, user_id, group_id, admin_id, reason)

    await conn.close()


# -----------------------------
# JOIN GUARD
# -----------------------------

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Call this from a ChatMemberHandler (JOIN events).
    """
    chat = update.effective_chat
    member = update.chat_member.new_chat_member.user
    user_id = member.id
    group_id = chat.id

    # Admins & bots bypass this system
    if member.is_bot:
        return

    # Check override
    if await has_override(user_id, group_id):
        return

    ban_count = await active_ban_count(user_id)

    if ban_count < BAN_THRESHOLD:
        return

    # Block user
    await context.bot.ban_chat_member(
        chat_id=group_id,
        user_id=user_id
    )

    # Alert admins
    text = (
        "🚨 **User blocked by Eminence reputation system**\n\n"
        f"User: [{member.first_name}](tg://user?id={user_id})\n"
        f"Active bans: {ban_count}\n\n"
        "Admins may verify and use /unmute to allow entry."
    )

    await context.bot.send_message(
        chat_id=group_id,
        text=text,
        parse_mode="Markdown"
    )


# -----------------------------
# ADMIN BYPASS COMMAND
# -----------------------------

async def unmute_override(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only.
    Works when replying to a blocked user join message.
    """
    if not update.message.reply_to_message:
        return

    admin = update.effective_user
    group_id = update.effective_chat.id

    # Extract user from reply (Telegram join message)
    target = update.message.reply_to_message.from_user
    if not target:
        return

    await create_override(
        user_id=target.id,
        group_id=group_id,
        admin_id=admin.id,
        reason="Manual admin override"
    )

    await context.bot.unban_chat_member(
        chat_id=group_id,
        user_id=target.id,
        only_if_banned=True
    )

    await update.message.reply_text(
        "✅ Override applied. User may now join this group."
    )
