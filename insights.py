import datetime
import redis
from telegram import Update
from telegram.ext import (
    ContextTypes,
    MessageHandler,
    CommandHandler,
    MessageReactionHandler,
    filters,
)

# ==============================
# Redis Setup
# ==============================

r = redis.Redis(host="localhost", port=6379, db=0)


# ==============================
# Redis Key Helper
# ==============================

def lifetime_key(chat_id, key):
    return f"insight:{chat_id}:{key}"


# ==============================
# Activity Tracking (Lifetime)
# ==============================

def add_activity(chat_id, user_id, points):
    r.zincrby(lifetime_key(chat_id, "activity_points"), points, user_id)


def increment_total_activity(chat_id, points):
    r.incrbyfloat(lifetime_key(chat_id, "total_activity"), points)


def set_start_date_if_missing(chat_id):
    key = lifetime_key(chat_id, "start_date")
    if not r.get(key):
        today = datetime.date.today().isoformat()
        r.set(key, today)


# ==============================
# Handlers
# ==============================

async def track_lifetime_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message or not message.from_user:
        return

    if message.from_user.is_bot:
        return

    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    set_start_date_if_missing(chat_id)

    add_activity(chat_id, user_id, 1.0)
    increment_total_activity(chat_id, 1.0)


async def track_lifetime_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            user_id = user.id

            set_start_date_if_missing(chat_id)

            add_activity(chat_id, user_id, 0.5)
            increment_total_activity(chat_id, 0.5)


async def track_lifetime_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            user_id = user.id

            set_start_date_if_missing(chat_id)

            add_activity(chat_id, user_id, 0.5)
            increment_total_activity(chat_id, 0.5)


# ==============================
# /insights Command
# ==============================

async def insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id
    group_name = chat.title or "This Group"

    start_date_raw = r.get(lifetime_key(chat_id, "start_date"))
    total_activity_raw = r.get(lifetime_key(chat_id, "total_activity"))

    if not start_date_raw:
        await update.message.reply_text(
            f"📊 {group_name} — Insight\n\nNo historical data available yet."
        )
        return

    start_date = start_date_raw.decode()
    total_activity = float(total_activity_raw or 0)

    top_user = r.zrevrange(
        lifetime_key(chat_id, "activity_points"),
        0,
        0,
        withscores=True
    )

    if not top_user or total_activity == 0:
        await update.message.reply_text(
            f"📊 {group_name} — Insight\n\n"
            f"Start Date: {start_date}\n"
            f"Total Activity: {int(total_activity)} points"
        )
        return

    user_id_bytes, points = top_user[0]
    user_id = int(user_id_bytes.decode())
    percentage = (points / total_activity) * 100

    # Fetch user info for proper name
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        user = member.user
        display_name = user.full_name
        mention = f"[{display_name}](tg://user?id={user_id})"
    except:
        mention = "Unknown User"

    await update.message.reply_text(
        f"📊 {group_name} — Insight\n\n"
        f"Start Date: {start_date}\n"
        f"Total Activity: {int(total_activity)} points\n\n"
        f"🏆 Most Active Member:\n"
        f"{mention} — {round(percentage, 2)}% of all activity",
        parse_mode="Markdown"
    )


# ==============================
# Registration Function
# ==============================

def register_insight_handlers(app):
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, track_lifetime_messages)
    )
    app.add_handler(MessageReactionHandler(track_lifetime_reactions))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, track_lifetime_polls))
    app.add_handler(CommandHandler("insights", insights))
