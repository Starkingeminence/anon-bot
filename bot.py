import os
import threading
import asyncio
from flask import Flask
import psycopg2
from psycopg2.extras import DictCursor
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- FAKE WEBSITE ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!"


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")


# --- Database helpers (synchronous, used via asyncio.to_thread) ---
def _open_conn():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set")
    # sslmode='require' is kept to match original; remove if not needed by your DB host
    return psycopg2.connect(DB_URL, cursor_factory=DictCursor, sslmode="require")


def get_next_anon_id_sync(group_id: int) -> str:
    conn = _open_conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS anon_counters (
                    group_id BIGINT PRIMARY KEY,
                    counter INT DEFAULT 0
                );
                """
            )
            cur.execute("SELECT counter FROM anon_counters WHERE group_id = %s", (group_id,))
            row = cur.fetchone()
            if row:
                counter = row["counter"] + 1
                cur.execute("UPDATE anon_counters SET counter = %s WHERE group_id = %s", (counter, group_id))
            else:
                counter = 1
                cur.execute("INSERT INTO anon_counters (group_id, counter) VALUES (%s, %s)", (group_id, counter))
            return f"A{counter}"
    finally:
        conn.close()


def store_anon_message_sync(group_id: int, anon_id: str, user_id: int, text: str) -> None:
    conn = _open_conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS anon_messages (
                    id SERIAL PRIMARY KEY,
                    group_id BIGINT,
                    anon_id VARCHAR(50),
                    user_id BIGINT,
                    message_text TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cur.execute(
                "INSERT INTO anon_messages (group_id, anon_id, user_id, message_text) VALUES (%s, %s, %s, %s)",
                (group_id, anon_id, user_id, text),
            )
    finally:
        conn.close()


def trace_anon_sync(group_id: int, anon_id: str):
    conn = _open_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM anon_messages WHERE group_id = %s AND anon_id = %s", (group_id, anon_id))
            row = cur.fetchone()
            return row["user_id"] if row else None
    finally:
        conn.close()


# --- Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Online. Use /anon <message> to post.")


async def anon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = " ".join(context.args).strip()
    if not message_text:
        await update.message.reply_text("Usage: /anon <your message>")
        return

    user_id = update.message.from_user.id
    chat_id = update.message.chat.id

    try:
        # Use asyncio.to_thread to avoid blocking the event loop on psycopg2 calls
        anon_id = await asyncio.to_thread(get_next_anon_id_sync, chat_id)

        # Save message
        await asyncio.to_thread(store_anon_message_sync, chat_id, anon_id, user_id, message_text)

        # Post anonymous message
        await context.bot.send_message(chat_id=chat_id, text=f"🕶 Anonymous #{anon_id}:\n{message_text}")

        # Try to delete the user's original message (best-effort)
        try:
            await update.message.delete()
        except Exception:
            pass

    except Exception as e:
        # Avoid leaking internal DB connection strings or secrets in production logs/replies
        print(f"[ERROR] anon command failed: {e}")
        await update.message.reply_text("⚠️ An internal error occurred.")


async def trace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only admins allowed
    chat_admins = await update.effective_chat.get_administrators()
    if update.message.from_user.id not in [a.user.id for a in chat_admins]:
        await update.message.reply_text("❌ Admins only.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /trace <anon_id>")
        return

    anon_id = context.args[0]
    chat_id = update.message.chat.id

    try:
        user_id = await asyncio.to_thread(trace_anon_sync, chat_id, anon_id)
        if user_id:
            await update.message.reply_text(f"🕵️ #{anon_id} is User ID: {user_id}")
        else:
            await update.message.reply_text("❌ Not found.")
    except Exception as e:
        print(f"[ERROR] trace command failed: {e}")
        await update.message.reply_text("⚠️ An internal error occurred.")


if __name__ == "__main__":
    # Start the fake website in the background
    threading.Thread(target=run_web_server, daemon=True).start()

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("anon", anon))
    application.add_handler(CommandHandler("trace", trace))
    application.run_polling()
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
