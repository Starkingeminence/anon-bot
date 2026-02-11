import os
import datetime
import redis
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
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

# ==============================
# Utility Functions
# ==============================
def lifetime_key(chat_id, key_type):
    return f"insight:{chat_id}:{key_type}"

def add_activity(chat_id, user_id, points=1):
    r.zincrby(lifetime_key(chat_id, "activity_points"), points, user_id)

def mark_start_date(chat_id):
    key = lifetime_key(chat_id, "start_date")
    if not r.get(key):
        r.set(key, datetime.date.today().isoformat())

def get_lifetime_data(chat_id):
    start_date_str = r.get(lifetime_key(chat_id, "start_date"))
    start_date = datetime.datetime.strptime(start_date_str.decode(), "%Y-%m-%d").date() if start_date_str else None
    total_activity = r.zscore(lifetime_key(chat_id, "activity_points"), "__total__") or 0
    top_users = r.zrevrange(lifetime_key(chat_id, "activity_points"), 0, 2, withscores=True)
    return start_date, total_activity, top_users

# ==============================
# Handlers
# ==============================
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if user.is_bot:
        return

    # Exclude bot commands
    if update.message and update.message.text and update.message.text.startswith("/"):
        return

    mark_start_date(chat_id)

    points = 1
    # Poll vote or reaction counts as 0.5
    if hasattr(update, "poll_answer") and update.poll_answer:
        points = 0.5
    elif hasattr(update, "message_reaction") and update.message_reaction:
        points = 0.5

    add_activity(chat_id, user.id, points)

async def insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    start_date, total_activity, top_users = get_lifetime_data(chat_id)

    if total_activity == 0:
        await update.message.reply_text(
            f"📊 Insights\nStart Date: {start_date}\nNo activity recorded yet."
        )
        return

    # Prepare top contributor text with percentages
    text = ""
    for i, (uid, points) in enumerate(top_users):
        percent = (points / total_activity) * 100 if total_activity else 0
        text += f"{i+1}. [{uid.decode()}] — {round(percent, 2)}%\n"

    await update.message.reply_text(
        f"📊 Group Insights (All-Time)\n"
        f"Start Date: {start_date}\n"
        f"Total Activity Points: {int(total_activity)}\n"
        f"Top 3 Contributors (messages + polls + reactions combined):\n{text}"
    )

# ==============================
# Main App
# ==============================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_activity))
    app.add_handler(MessageHandler(filters.ALL, track_activity))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, track_activity))
    app.add_handler(CommandHandler("insights", insights))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
