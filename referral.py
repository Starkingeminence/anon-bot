import asyncio
import time
import json
import redis
from datetime import datetime, timedelta
from telegram import Update, ChatInviteLink
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ChatMemberHandler,
)

# ==============================
# Redis Setup
# ==============================

r = redis.Redis(host="localhost", port=6379, db=0)


# ==============================
# Key Helpers
# ==============================

def key(chat_id, suffix):
    return f"ref:{chat_id}:{suffix}"


# ==============================
# Owner Check
# ==============================

async def is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user_id = update.effective_user.id
    member = await context.bot.get_chat_member(chat.id, user_id)
    return member.status == "creator"


# ==============================
# Start Event (Owner Only - Private)
# ==============================

async def referral_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    if not context.args:
        await update.message.reply_text("Usage: /referral_event_start <group_id>")
        return

    chat_id = int(context.args[0])

    # Check ownership
    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if member.status != "creator":
        await update.message.reply_text("Only group owner can start referral event.")
        return

    if r.get(key(chat_id, "active")):
        await update.message.reply_text("Referral event already active.")
        return

    duration_days = 7
    end_time = int(time.time()) + duration_days * 86400

    settings = {
        "min_stay_hours": 24,
        "removal_penalty": False
    }

    r.set(key(chat_id, "active"), 1)
    r.set(key(chat_id, "end_time"), end_time)
    r.set(key(chat_id, "settings"), json.dumps(settings))

    await update.message.reply_text("Referral event started for 7 days.")


# ==============================
# Stop Event
# ==============================

async def referral_event_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    if not context.args:
        await update.message.reply_text("Usage: /referral_event_stop <group_id>")
        return

    chat_id = int(context.args[0])

    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if member.status != "creator":
        await update.message.reply_text("Only group owner can stop referral event.")
        return

    r.delete(key(chat_id, "active"))
    r.delete(key(chat_id, "end_time"))
    r.delete(key(chat_id, "settings"))

    await update.message.reply_text("Referral event stopped.")


# ==============================
# Generate Referral Link
# ==============================

async def my_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    if not context.args:
        await update.message.reply_text("Usage: /my_referral_link <group_id>")
        return

    chat_id = int(context.args[0])
    user_id = update.effective_user.id

    if not r.get(key(chat_id, "active")):
        await update.message.reply_text("No active referral event.")
        return

    existing = r.get(key(chat_id, f"user_link:{user_id}"))
    if existing:
        await update.message.reply_text(f"Your referral link:\n{existing.decode()}")
        return

    invite: ChatInviteLink = await context.bot.create_chat_invite_link(
        chat_id=chat_id,
        creates_join_request=False
    )

    r.set(key(chat_id, f"link:{invite.invite_link}"), user_id)
    r.set(key(chat_id, f"user_link:{user_id}"), invite.invite_link)

    await update.message.reply_text(f"Your referral link:\n{invite.invite_link}")


# ==============================
# Track Joins
# ==============================

async def track_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member

    if result.new_chat_member.status != "member":
        return

    chat_id = result.chat.id

    if not r.get(key(chat_id, "active")):
        return

    invite = result.invite_link
    if not invite:
        return

    referrer = r.get(key(chat_id, f"link:{invite.invite_link}"))
    if not referrer:
        return

    new_user_id = result.new_chat_member.user.id

    if r.sismember(key(chat_id, "qualified_users"), new_user_id):
        return

    pending_data = {
        "referrer": int(referrer.decode()),
        "joined_at": int(time.time())
    }

    r.set(key(chat_id, f"pending:{new_user_id}"), json.dumps(pending_data))


# ==============================
# Background Qualification Task
# ==============================

async def referral_scheduler(app):
    while True:
        now = int(time.time())

        for k in r.scan_iter("ref:*:active"):
            chat_id = int(k.decode().split(":")[1])
            settings = json.loads(r.get(key(chat_id, "settings")))
            min_stay = settings["min_stay_hours"] * 3600

            for pending_key in r.scan_iter(key(chat_id, "pending:*")):
                new_user_id = int(pending_key.decode().split(":")[-1])
                data = json.loads(r.get(pending_key))
                joined_at = data["joined_at"]

                if now - joined_at >= min_stay:
                    referrer_id = data["referrer"]

                    r.zincrby(key(chat_id, "score"), 1, referrer_id)
                    r.sadd(key(chat_id, "qualified_users"), new_user_id)

                    r.delete(pending_key)

                    try:
                        rank = r.zrevrank(key(chat_id, "score"), referrer_id) + 1
                        await app.bot.send_message(
                            referrer_id,
                            f"🎉 New Qualified Referral!\n"
                            f"Your total referrals: {int(r.zscore(key(chat_id, 'score'), referrer_id))}\n"
                            f"Current Rank: #{rank}"
                        )
                    except:
                        pass

        await asyncio.sleep(300)
