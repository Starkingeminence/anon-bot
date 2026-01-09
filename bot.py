import asyncio
import time
import os
import psycopg2
from psycopg2.extras import DictCursor
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

# --- Flood control ---
user_last_post = {}
FLOOD_DELAY = 5

# --- Connect to Database ---
try:
    conn = psycopg2.connect(DB_URL, cursor_factory=DictCursor)
    conn.autocommit = True
    print("✅ Connected to Database")
except Exception as e:
    print(f"❌ Database connection failed: {e}")

# --- Helper functions ---
def get_next_anon_id(group_id):
    with conn.cursor() as cur:
        cur.execute("SELECT counter FROM anon_counters WHERE group_id=%s", (group_id,))
        row = cur.fetchone()
        if row:
            counter = row['counter'] + 1
            cur.execute("UPDATE anon_counters SET counter=%s WHERE group_id=%s", (counter, group_id))
        else:
            counter = 1
            cur.execute("INSERT INTO anon_counters (group_id, counter) VALUES (%s, %s)", (group_id, counter))
        return f"A{counter}"

def store_anon_message(group_id, anon_id, user_id, text):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anon_messages (group_id, anon_id, user_id, message_text) VALUES (%s,%s,%s,%s)",
            (group_id, anon_id, user_id, text)
        )

def trace_anon(group_id, anon_id):
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM anon_messages WHERE group_id=%s AND anon_id=%s", (group_id, anon_id))
        row = cur.fetchone()
        return row['user_id'] if row else None

# --- Bot commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Online. Use /anon <message> to post.")

async def anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = " ".join(context.args)
    if not message_text:
        await update.message.reply_text("Usage: /anon <your message>")
        return

    user_id = update.message.from_user.id
    chat_id = update.message.chat.id

    # Flood Check
    now = time.time()
    if user_id in user_last_post and now - user_last_post[user_id] < FLOOD_DELAY:
        await update.message.reply_text("⏱ Wait a few seconds.")
        return
    user_last_post[user_id] = now

    try:
        anon_id = get_next_anon_id(chat_id)
        store_anon_message(chat_id, anon_id, user_id, message_text)
        try:
            await update.message.delete()
        except:
            pass
        await context.bot.send_message(chat_id=chat_id, text=f"🕶 Anonymous #{anon_id}:\n{message_text}")
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("❌ Database error.")

async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_admins = await update.effective_chat.get_administrators()
    if update.message.from_user.id not in [a.user.id for a in chat_admins]:
        await update.message.reply_text("❌ Admins only.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /trace <anon_id>")
        return

    anon_id = context.args[0]
    chat_id = update.message.chat.id
    user_id = trace_anon(chat_id, anon_id)
    if user_id:
        await update.message.reply_text(f"🕵️ #{anon_id} is User ID: {user_id}")
    else:
        await update.message.reply_text("❌ Not found.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("anon", anon))
    app.add_handler(CommandHandler("trace", trace))
    app.run_polling()
                
