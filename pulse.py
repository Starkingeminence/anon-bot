import os
import math
import datetime
import redis
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ChatMemberHandler
)

# ==============================
# Load environment variables
# ==============================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ==============================
# Redis setup
# ==============================
r = redis.Redis(host='localhost', port=6379, db=0)

WEEK_SECONDS = 7 * 24 * 60 * 60


# ==============================
# Utility Functions
# ==============================

def week_key(chat_id, key_type):
    return f"pulse:{chat_id}:{key_type}"


def add_unique(chat_id, key_type, user_id):
    key = week_key(chat_id, key_type)
    r.sadd(key, user_id)
    r.expire(key, WEEK_SECONDS)


def increment_counter(chat_id, key_type):
    key = week_key(chat_id, key_type)
    r.incr(key)
    r.expire(key, WEEK_SECONDS)


def mark_active_day(chat_id):
    today = datetime.date.today().isoformat()
    key = week_key(chat_id, "active_days")
    r.sadd(key, today)
    r.expire(key, WEEK_SECONDS)


def get_weekly_data(chat_id):
    A_msg = r.scard(week_key(chat_id, "msg_users"))
    A_react = r.scard(week_key(chat_id, "react_users"))
    A_poll = r.scard(week_key(chat_id, "poll_users"))
    M = int(r.get(week_key(chat_id, "message_count")) or 0)
    active_days = r.scard(week_key(chat_id, "active_days"))

    return A_msg, A_react, A_poll, M, active_days


def calculate_pulse(G, A_msg, A_react, A_poll, M, active_days):
    effective_active = A_msg + (0.5 * A_react) + (0.5 * A_poll)
    P = effective_active / G if G > 0 else 0

    participation_score = min(max(P / 0.10, 0), 1)

    engagement_factor = 0
    if A_msg > 0:
        engagement_factor = min(
            max(math.log2(1 + (M / A_msg)) / math.log2(6), 0), 1
        )

    consistency = active_days / 7

    pulse_score = 100 * (
        0.7 * participation_score +
        0.2 * engagement_factor +
        0.1 * consistency
    )

    return round(pulse_score, 2)


def get_verdict(score):
    if score >= 60:
        return "🟢 Strong"
    elif score >= 40:
        return "🟡 Normal"
    elif score >= 20:
        return "🟠 Weak"
    else:
        return "⚫ Dead"


# ==============================
# Handlers
# ==============================

async def track_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.is_bot:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    add_unique(chat_id, "msg_users", user_id)
    increment_counter(chat_id, "message_count")
    mark_active_day(chat_id)


async def track_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            add_unique(chat_id, "react_users", user.id)
            mark_active_day(chat_id)


async def track_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if not user.is_bot:
            chat_id = update.effective_chat.id
            add_unique(chat_id, "poll_users", user.id)
            mark_active_day(chat_id)


async def pulse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id

    # 7-day cooldown check
    cooldown_key = week_key(chat_id, "last_pulse")
    last_used = r.get(cooldown_key)

    if last_used:
        await update.message.reply_text(
            "⏳ Pulse can only be used once every 7 days."
        )
        return

    G = await context.bot.get_chat_member_count(chat_id)

    A_msg, A_react, A_poll, M, active_days = get_weekly_data(chat_id)

    score = calculate_pulse(G, A_msg, A_react, A_poll, M, active_days)
    verdict = get_verdict(score)

    r.setex(cooldown_key, WEEK_SECONDS, 1)

    await update.message.reply_text(
        f"📊 Pulse Report\n\n"
        f"Score: {score}/100\n"
        f"Status: {verdict}\n\n"
        f"Members: {G}\n"
        f"Message Senders: {A_msg}\n"
        f"Reactors: {A_react}\n"
        f"Poll Voters: {A_poll}\n"
        f"Messages: {M}\n"
        f"Active Days: {active_days}/7"
    )


# ==============================
# Main App
# ==============================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_messages))
    app.add_handler(MessageHandler(filters.ALL, track_reactions))
    app.add_handler(CommandHandler("pulse", pulse))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, track_polls))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
