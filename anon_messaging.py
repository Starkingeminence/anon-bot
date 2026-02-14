"""
Anonymous Messaging Module

Responsibilities:
- Link user (DM) to group via deep link
- Forward DM messages anonymously
- Store trace logs
- Allow admins to trace messages
- Show current linked group
"""

import os
import asyncpg
import logging

from telethon import TelegramClient, events
from telethon.errors import RPCError

logger = logging.getLogger(__name__)

# ----------------------------
# Environment
# ----------------------------

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ----------------------------
# Client
# ----------------------------

client = TelegramClient("anon_session", API_ID, API_HASH)

# In-memory cache
USER_SESSIONS = {}  # user_id -> group_id


class AnonymousMessaging:

    def __init__(self):
        self.pool = None

    # -------------------- DB --------------------

    async def connect_db(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Anon DB pool connected ✅")

    # -------------------- Linking --------------------

    async def link_user(self, user_id: int, group_id: int):
        USER_SESSIONS[user_id] = group_id

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO anon_connections (user_id, group_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id)
                DO UPDATE SET group_id = EXCLUDED.group_id
            """, user_id, group_id)

    async def get_linked_group(self, user_id: int):
        if user_id in USER_SESSIONS:
            return USER_SESSIONS[user_id]

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT group_id FROM anon_connections WHERE user_id = $1",
                user_id
            )

        if row:
            USER_SESSIONS[user_id] = row["group_id"]
            return row["group_id"]

        return None

    # -------------------- Sending --------------------

    async def send_anonymous(self, user_id: int, text: str):
        group_id = await self.get_linked_group(user_id)

        if not group_id:
            return False, "❌ You are not connected to any group."

        try:
            group = await client.get_entity(group_id)

            sent = await client.send_message(
                group_id,
                f"🕶 <b>Anonymous</b>\n\n{text}",
                parse_mode="html"
            )

            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO anon_logs (group_id, message_id, user_id)
                    VALUES ($1, $2, $3)
                """, group_id, sent.id, user_id)

            return True, f"✅ Sent anonymously to <b>{group.title}</b>"

        except RPCError:
            logger.exception("Anonymous send failed (RPC)")
            return False, "❌ Failed to send anonymous message."
        except Exception:
            logger.exception("Anonymous send failed (General)")
            return False, "❌ Failed to send anonymous message."

    # -------------------- Trace --------------------

    async def trace_message(self, group_id: int, message_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT user_id
                FROM anon_logs
                WHERE group_id = $1 AND message_id = $2
            """, group_id, message_id)

        return row["user_id"] if row else None


anon = AnonymousMessaging()


# ====================================================
# ----------------- TELETHON HANDLERS ----------------
# ====================================================

def register_anon_handlers():

    # ----------- DM Message Forwarding -----------

    @client.on(events.NewMessage(incoming=True, private=True))
    async def anon_dm_handler(event):

        if not event.text:
            return

        if event.text.startswith("/"):
            return

        success, reply = await anon.send_anonymous(
            event.sender_id,
            event.text
        )

        await event.reply(reply, parse_mode="html")

    # ----------- Deep Link /start -----------

    @client.on(events.NewMessage(pattern=r"^/start (\-?\d+)$"))
    async def start_handler(event):

        if not event.is_private:
            return

        group_id = int(event.pattern_match.group(1))
        await anon.link_user(event.sender_id, group_id)

        await event.reply(
            "🕶 <b>Anonymous Messaging Enabled</b>\n\n"
            "Messages you send here will be forwarded anonymously.\n"
            "Admins can trace abuse.\n\n"
            "📌 Pin this chat for quick access.",
            parse_mode="html"
        )

    # ----------- /current_group -----------

    @client.on(events.NewMessage(pattern="^/current_group$"))
    async def current_group_handler(event):

        if not event.is_private:
            return

        group_id = await anon.get_linked_group(event.sender_id)

        if not group_id:
            await event.reply(
                "❌ You are not connected to any group.\n"
                "Ask an admin for the anonymous link."
            )
            return

        try:
            group = await client.get_entity(group_id)
            await event.reply(
                f"🔗 Connected to:\n<b>{group.title}</b>",
                parse_mode="html"
            )
        except Exception:
            await event.reply(
                "⚠️ Connected to a group, but I cannot fetch details."
            )

    # ----------- /trace (Group Only) -----------

    @client.on(events.NewMessage(pattern="^/trace$"))
    async def trace_handler(event):

        if event.is_private:
            return

        if not event.reply_to_msg_id:
            return

        # Admin check
        sender = await event.get_sender()
        permissions = await client.get_permissions(event.chat_id, sender.id)

        if not permissions.is_admin:
            return

        traced_user = await anon.trace_message(
            event.chat_id,
            event.reply_to_msg_id
        )

        if traced_user:
            await event.reply(
                f"🕵️ <a href='tg://user?id={traced_user}'>User Profile</a>",
                parse_mode="html"
            )
        else:
            await event.reply("⚠️ No anonymous record found.")


# ====================================================
# ---------------- START FUNCTION --------------------
# ====================================================

async def start_anon_client():
    await anon.connect_db()

    await client.start(bot_token=BOT_TOKEN)
    register_anon_handlers()

    logger.info("Anonymous Telethon client started ✅")

    await client.run_until_disconnected()
