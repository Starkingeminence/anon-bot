import math
import time
import json
import asyncio
import datetime
import re
import os

import redis.asyncio as redis
from telegram import Update
from telegram import ChatMemberUpdated
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    ChatMemberHandler,
    PollHandler,
    filters,
)

# ==========================================
# Redis Setup
# ==========================================
REDIS_URL = os.getenv("REDIS_URL")

r = redis.from_url(
    REDIS_URL,
    decode_responses=True
)

WEEK_SECONDS = 7 * 24 * 60 * 60


# ==========================================
# ---------- UTILITIES ----------
# ==========================================
def escape_markdown(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)


# ==========================================
# ---------------- PULSE -------------------
# ==========================================
def pulse_key(chat_id, key_type):
    return f"pulse:{chat_id}:{key_type}"


async def add_weekly_unique(chat_id, key_type, user_id):
    key = pulse_key(chat_id, key_type)
    await r.sadd(key, user_id)
    await r.expire(key, WEEK_SECONDS)


async def increment_weekly_counter(chat_id, key_type):
    key = pulse_key(chat_id, key_type)
    await r.incr(key)
    await r.expire(key, WEEK_SECONDS)


async def mark_weekly_active_day(chat_id):
    today = datetime.date.today().isoformat()
    key = pulse_key(chat_id, "active_days")
    await r.sadd(key, today)
    await r.expire(key, WEEK_SECONDS)


async def get_weekly_data(chat_id):
    A_msg = await r.scard(pulse_key(chat_id, "msg_users"))
    A_react = await r.scard(pulse_key(chat_id, "react_users"))
    A_poll = await r.scard(pulse_key(chat_id, "poll_users"))
    M = int(await r.get(pulse_key(chat_id, "message_count")) or 0)
    active_days = await r.scard(pulse_key(chat_id, "active_days"))
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
    if not message or not message.from_user or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    await add_weekly_unique(chat_id, "msg_users", user_id)
    await increment_weekly_counter(chat_id, "message_count")
    await mark_weekly_active_day(chat_id)


async def track_weekly_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            await add_weekly_unique(chat_id, "react_users", user.id)
            await mark_weekly_active_day(chat_id)


async def track_weekly_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            await add_weekly_unique(chat_id, "poll_users", user.id)
            await mark_weekly_active_day(chat_id)


# ---------- /pulse ----------
async def pulse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cooldown_key = pulse_key(chat_id, "last_pulse")

    if await r.get(cooldown_key):
        await update.message.reply_text(
            "⏳ Pulse can only be used once every 7 days."
        )
        return

    G = await context.bot.get_chat_member_count(chat_id)
    A_msg, A_react, A_poll, M, active_days = await get_weekly_data(chat_id)

    score = calculate_pulse(G, A_msg, A_react, A_poll, M, active_days)
    verdict = get_pulse_verdict(score, M)

    await r.setex(cooldown_key, WEEK_SECONDS, 1)

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


async def add_lifetime_activity(chat_id, user_id, points):
    await r.zincrby(insight_key(chat_id, "activity_points"), points, user_id)


async def increment_total_activity(chat_id, points):
    await r.incrbyfloat(insight_key(chat_id, "total_activity"), points)


async def set_start_date_if_missing(chat_id):
    key = insight_key(chat_id, "start_date")
    if not await r.get(key):
        today = datetime.date.today().isoformat()
        await r.set(key, today)


# ---------- Lifetime Tracking ----------
async def track_lifetime_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.from_user or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = message.from_user.id

    await set_start_date_if_missing(chat_id)
    await add_lifetime_activity(chat_id, user_id, 1.0)
    await increment_total_activity(chat_id, 1.0)


async def track_lifetime_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message_reaction:
        user = update.message_reaction.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            await set_start_date_if_missing(chat_id)
            await add_lifetime_activity(chat_id, user.id, 0.5)
            await increment_total_activity(chat_id, 0.5)


async def track_lifetime_polls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        user = update.poll_answer.user
        if user and not user.is_bot:
            chat_id = update.effective_chat.id
            await set_start_date_if_missing(chat_id)
            await add_lifetime_activity(chat_id, user.id, 0.5)
            await increment_total_activity(chat_id, 0.5)


# ---------- /insights ----------
async def insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id
    group_name = chat.title or "This Group"

    start_date = await r.get(insight_key(chat_id, "start_date"))
    total_activity_raw = await r.get(insight_key(chat_id, "total_activity"))

    if not start_date:
        await update.message.reply_text(
            f"📊 {group_name} — Insight\n\nNo historical data yet."
        )
        return

    total_activity = float(total_activity_raw or 0)

    top_user = await r.zrevrange(
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

    user_id, points = top_user[0]
    user_id = int(user_id)

    percentage = (points / total_activity) * 100

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        user = member.user
        mention = f"[{escape_markdown(user.full_name)}](tg://user?id={user_id})"
    except:
        mention = "Unknown User"

    await update.message.reply_text(
        f"📊 {group_name} — Insight\n\n"
        f"Start Date: {start_date}\n"
        f"Total Activity: {int(total_activity)} points\n\n"
        f"🏆 Most Active Member:\n"
        f"{mention} — {round(percentage, 2)}%",
        parse_mode="MarkdownV2"
    )


# ==========================================
# ---------------- REFERRAL ----------------
# ==========================================
def ref_key(chat_id, suffix):
    return f"ref:{chat_id}:{suffix}"


async def referral_scheduler(app):
    while True:
        now = int(time.time())

        async for k in r.scan_iter("ref:*:active"):
            chat_id = int(k.split(":")[1])

            settings_raw = await r.get(ref_key(chat_id, "settings"))
            if not settings_raw:
                continue

            settings = json.loads(settings_raw)
            min_stay = settings["min_stay_hours"] * 3600

            async for pending_key in r.scan_iter(ref_key(chat_id, "pending:*")):
                data_raw = await r.get(pending_key)
                if not data_raw:
                    continue

                data = json.loads(data_raw)
                joined_at = data["joined_at"]

                if now - joined_at >= min_stay:
                    referrer_id = data["referrer"]
                    new_user_id = int(pending_key.split(":")[-1])

                    await r.zincrby(ref_key(chat_id, "score"), 1, referrer_id)
                    await r.sadd(ref_key(chat_id, "qualified_users"), new_user_id)
                    await r.delete(pending_key)

                    try:
                        rank = await r.zrevrank(ref_key(chat_id, "score"), referrer_id)
                        total = await r.zscore(ref_key(chat_id, "score"), referrer_id)

                        await app.bot.send_message(
                            referrer_id,
                            f"🎉 Qualified Referral!\n"
                            f"Total: {int(total)}\n"
                            f"Rank: #{rank + 1}"
                        )
                    except:
                        pass

        await asyncio.sleep(300)


# Track user joins
async def track_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member: ChatMemberUpdated = update.chat_member
    user = chat_member.new_chat_member.user

    if user.is_bot:
        return

    if chat_member.new_chat_member.status == "member":
        print(f"User {user.id} joined chat {update.effective_chat.id}")


# ==========================================
# ----------- REGISTER HANDLERS ------------
# ==========================================
def register_analytics_handlers(app):
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_weekly_messages))
    app.add_handler(MessageReactionHandler(track_weekly_reactions))
    app.add_handler(PollHandler(track_weekly_polls))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_lifetime_messages))
    app.add_handler(MessageReactionHandler(track_lifetime_reactions))
    app.add_handler(PollHandler(track_lifetime_polls))

    app.add_handler(CommandHandler("pulse", pulse))
    app.add_handler(CommandHandler("insights", insights))

    app.add_handler(ChatMemberHandler(track_joins, ChatMemberHandler.CHAT_MEMBER))
