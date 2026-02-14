import math
import time
import json
import asyncio
import datetime
import redis

from telegram import Update, ChatInviteLink
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    ChatMemberHandler,
    filters,
)

# ==========================================
# Redis Setup
# ==========================================

r = redis.Redis(host="localhost", port=6379, db=0)
WEEK_SECONDS = 7 * 24 * 60 * 60


# ==========================================
# ---------------- PULSE -------------------
# ==========================================

def pulse_key(chat_id, key_type):
    return f"pulse:{chat_id}:{key_type}"


def add_weekly_unique(chat_id, key_type, user_id):
    key = pulse_key(chat_id, key_type)
    r.sadd(key, user_id)
    r.expire(key, WEEK_SECONDS)


def increment_weekly_counter(chat_id, key_type):
    key = pulse_key(chat_id, key_type)
    r.incr(key)
    r.expire(key, WEEK_SECONDS)


def mark_weekly_active_day(chat_id):
    today = datetime.date.today().isoformat()
    key = pulse_key(chat_id, "active_days")
    r.sadd(key, today)
    r.expire(key, WEEK_SECONDS)


def get_weekly_data(chat_id):
    A_msg = r.scard(pulse_key(chat_id, "msg_users"))
    A_react = r.scard(pulse_key(chat_id, "react_users"))
    A_poll = r.scard(pulse_key(chat_id, "poll_users"))
    M = int(r.get(pulse_key(chat_id, "message_count")) or 0)
    active_days = r.scard(pulse_key(chat_id, "active_days"))
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
        if (M / A_msg) < 2:
            engagement_factor = max(engagement_factor - 0.2, 0)

    consistency = active_days / 7

    pulse_score = 100 * (
        0.7 * participation_score +
        0.2 * engagement_factor +
        0.1 * consistency
    )

    return round(pulse_score, 2)


def get_pulse_verdict(score, total_messages):
    if total_messages == 0:
        return "⚫ Inactive"
    if score >= 60:
        return "🟢 Strong"
    elif score >= 40:
        return "🟡 Normal"
    elif score >= 20:
        return "🟠 Weak"
    return "🔵 Faint"


# ---------- Weekly Tracking ----------

async def track_weekly_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.from_user:
        return

    if message.from_user.is_bot:
        return

    if message.text and message.text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    add_weekly_unique(chat_id, "msg_users", user_id)
    increment_weekly_counter(chat_id, "message_count")
    mark_weekly_active_day(chat_id)


async def track_weekly_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            add_weekly_unique(chat_id, "react_users", user.id)
            mark_weekly_active_day(chat_id)


async def track_weekly_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            add_weekly_unique(chat_id, "poll_users", user.id)
            mark_weekly_active_day(chat_id)


# ---------- /pulse ----------

async def pulse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    cooldown_key = pulse_key(chat_id, "last_pulse")
    if r.get(cooldown_key):
        await update.message.reply_text(
            "⏳ Pulse can only be used once every 7 days."
        )
        return

    G = await context.bot.get_chat_member_count(chat_id)
    A_msg, A_react, A_poll, M, active_days = get_weekly_data(chat_id)

    score = calculate_pulse(G, A_msg, A_react, A_poll, M, active_days)
    verdict = get_pulse_verdict(score, M)

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


# ==========================================
# ---------------- INSIGHTS ----------------
# ==========================================

def insight_key(chat_id, key):
    return f"insight:{chat_id}:{key}"


def add_lifetime_activity(chat_id, user_id, points):
    r.zincrby(insight_key(chat_id, "activity_points"), points, user_id)


def increment_total_activity(chat_id, points):
    r.incrbyfloat(insight_key(chat_id, "total_activity"), points)


def set_start_date_if_missing(chat_id):
    key = insight_key(chat_id, "start_date")
    if not r.get(key):
        today = datetime.date.today().isoformat()
        r.set(key, today)


# ---------- Lifetime Tracking ----------

async def track_lifetime_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.from_user:
        return
    if message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    set_start_date_if_missing(chat_id)
    add_lifetime_activity(chat_id, user_id, 1.0)
    increment_total_activity(chat_id, 1.0)


async def track_lifetime_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            set_start_date_if_missing(chat_id)
            add_lifetime_activity(chat_id, user.id, 0.5)
            increment_total_activity(chat_id, 0.5)


async def track_lifetime_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            set_start_date_if_missing(chat_id)
            add_lifetime_activity(chat_id, user.id, 0.5)
            increment_total_activity(chat_id, 0.5)


# ---------- /insights ----------

async def insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id
    group_name = chat.title or "This Group"

    start_date_raw = r.get(insight_key(chat_id, "start_date"))
    total_activity_raw = r.get(insight_key(chat_id, "total_activity"))

    if not start_date_raw:
        await update.message.reply_text(
            f"📊 {group_name} — Insight\n\nNo historical data yet."
        )
        return

    start_date = start_date_raw.decode()
    total_activity = float(total_activity_raw or 0)

    top_user = r.zrevrange(
        insight_key(chat_id, "activity_points"),
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

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        user = member.user
        mention = f"[{user.full_name}](tg://user?id={user_id})"
    except:
        mention = "Unknown User"

    await update.message.reply_text(
        f"📊 {group_name} — Insight\n\n"
        f"Start Date: {start_date}\n"
        f"Total Activity: {int(total_activity)} points\n\n"
        f"🏆 Most Active Member:\n"
        f"{mention} — {round(percentage, 2)}%",
        parse_mode="Markdown"
    )


# ==========================================
# ---------------- REFERRAL ----------------
# ==========================================

def ref_key(chat_id, suffix):
    return f"ref:{chat_id}:{suffix}"


async def referral_scheduler(app):
    while True:
        now = int(time.time())

        for k in r.scan_iter("ref:*:active"):
            chat_id = int(k.decode().split(":")[1])
            settings = json.loads(r.get(ref_key(chat_id, "settings")))
            min_stay = settings["min_stay_hours"] * 3600

            for pending_key in r.scan_iter(ref_key(chat_id, "pending:*")):
                new_user_id = int(pending_key.decode().split(":")[-1])
                data = json.loads(r.get(pending_key))
                joined_at = data["joined_at"]

                if now - joined_at >= min_stay:
                    referrer_id = data["referrer"]

                    r.zincrby(ref_key(chat_id, "score"), 1, referrer_id)
                    r.sadd(ref_key(chat_id, "qualified_users"), new_user_id)
                    r.delete(pending_key)

                    try:
                        rank = r.zrevrank(
                            ref_key(chat_id, "score"),
                            referrer_id
                        ) + 1

                        await app.bot.send_message(
                            referrer_id,
                            f"🎉 Qualified Referral!\n"
                            f"Total: {int(r.zscore(ref_key(chat_id,'score'), referrer_id))}\n"
                            f"Rank: #{rank}"
                        )
                    except:
                        pass

        await asyncio.sleep(300)


# ==========================================
# ----------- REGISTRATION -----------------
# ==========================================

def register_analytics_handlers(app):
    # Weekly tracking
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, track_weekly_messages)
    )
    app.add_handler(MessageReactionHandler(track_weekly_reactions))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, track_weekly_polls))

    # Lifetime tracking
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, track_lifetime_messages)
    )
    app.add_handler(MessageReactionHandler(track_lifetime_reactions))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, track_lifetime_polls))

    # Commands
    app.add_handler(CommandHandler("pulse", pulse))
    app.add_handler(CommandHandler("insights", insights))

    # Referral join tracking
    app.add_handler(ChatMemberHandler(track_joins, ChatMemberHandler.CHAT_MEMBER))

def register_moderation_handlers(app):
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(MessageHandler(filters.ALL, moderation_guard))

