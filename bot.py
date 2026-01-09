import time
import os
import psycopg2
import threading
from flask import Flask
from psycopg2.extras import DictCursor
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- FAKE WEBSITE ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

# --- Database Connection ---
conn = None
def get_db_connection():
    global conn
    try:
        if conn is None or conn.closed != 0:
            print("🔄 Connecting to Database...")
            conn = psycopg2.connect(DB_URL, cursor_factory=DictCursor, sslmode='require')
            conn.autocommit = True
            print("✅ Database Connected")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return f"ERROR: {e}"
    return conn

# --- Helper functions ---
def get_next_anon_id(group_id):
    c = get_db_connection()
    if isinstance(c, str): return c 
    if not c: return "ERR_NO_CONN"
        
    with c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS anon_counters (group_id BIGINT PRIMARY KEY, counter INT DEFAULT 0);")
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
    c = get_db_connection()
    if not c or isinstance(c, str): return
    
    with c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS anon_messages (
                id SERIAL PRIMARY KEY,
                group_id BIGINT,
                anon_id VARCHAR(50),
                user_id BIGINT,
                message_text TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute(
            "INSERT INTO anon_messages (group_id, anon_id, user_id, message_text) VALUES (%s,%s,%s,%s)",
            (group_id, anon_id, user_id, text)
        )

def trace_anon(group_id, anon_id):
    c = get_db_connection()
    if not c or isinstance(c, str): return None
    with c.cursor() as cur:
        cur.execute("SELECT user_id FROM anon_messages WHERE group_id=%s AND anon_id=%s", (group_id, anon_id))
        row = cur.fetchone()
        return row['user_id'] if row else None

# --- Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Online. Use /anon <message> to post.")

async def anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = " ".join(context.args)
    if not message_text:
        await update.message.reply_text("Usage: /anon <your message>")
        return

    user_id = update.message.from_user.id
    chat_id = update.message.chat.id

    try:
        # 1. Get ID
        anon_id = get_next_anon_id(chat_id)
        if "ERROR" in anon_id:
            await update.message.reply_text(f"⚠️ DEBUG: {anon_id}")
            return

        # 2. Save
        store_anon_message(chat_id, anon_id, user_id, message_text)
        
        # 3. Send
        await context.bot.send_message(chat_id=chat_id, text=f"🕶 Anonymous #{anon_id}:\n{message_text}")

        # 4. Delete (Simplified to avoid indentation errors)
        try:
            await update.message.delete()
        except:
            pass 
            
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text(f"⚠️ DEBUG ERROR: {e}")

async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_admins = await update.effective_chat.get_administrators()
    if update.message.from_user.id not in [a.user.id for a in chat_admins]:
        await update.message.reply_text("❌ Admins only.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /trace <anon_id>")
        return

    try:
        anon_id = context.args[0]
        result = trace_anon(update.message.chat.id, anon_id)
        if result:
            await update.message.reply_text(f"🕵️ #{anon_id} is User ID: {result}")
        else:
            await update.message.reply_text("❌ Not found.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ DEBUG ERROR: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("anon", anon))
    app.add_handler(CommandHandler("trace", trace))
    app.run_polling()
            await update.message.reply_text(f"⚠️ DEBUG: {anon_id}")
            return

        # Step 2: Save Message
        store_anon_message(chat_id, anon_id, user_id, message_text)
        
        # Step 3: Post Anon Message FIRST (so we know it worked)
        await context.bot.send_message(chat_id=chat_id, text=f"🕶 Anonymous #{anon_id}:\n{message_text}")

        # Step 4: Delete User Message (Safely)
        try:
            await update.message.delete()
        except Exception:
            pass
        # Step 1: Get ID
        anon_id = get_next_anon_id(chat_id)
        
        # If database failed, anon_id will hold the error message
        if "ERROR" in anon_id:
            await update.message.reply_text(f"⚠️ DEBUG: {anon_id}")
            return

        # Step 2: Save Message
        store_anon_message(chat_id, anon_id, user_id, message_text)
        
        # Step 3: Delete User Message
        try:
            await update.message.delete()
        except:
            pass 
            
        # Step 4: Post Anon Message
        await context.bot.send_message(chat_id=chat_id, text=f"🕶 Anonymous #{anon_id}:\n{message_text}")
        
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text(f"⚠️ DEBUG ERROR: {e}")

async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_admins = await update.effective_chat.get_administrators()
    if update.message.from_user.id not in [a.user.id for a in chat_admins]:
        await update.message.reply_text("❌ Admins only.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /trace <anon_id>")
        return

    try:
        anon_id = context.args[0]
        chat_id = update.message.chat.id
        user_id = trace_anon(chat_id, anon_id)
        if user_id:
            await update.message.reply_text(f"🕵️ #{anon_id} is User ID: {user_id}")
        else:
            await update.message.reply_text("❌ Not found.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ DEBUG ERROR: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("anon", anon))
    app.add_handler(CommandHandler("trace", trace))
    app.run_polling()
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
    # Start the fake website in the background
    threading.Thread(target=run_web_server, daemon=True).start()
    
    # Start the bot
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("anon", anon))
    app.add_handler(CommandHandler("trace", trace))
    app.run_polling()
