"""
Anonymous Messaging Module for Eminence DAO Bot

Responsibilities:
- Link a user (DM) to a group
- Forward DM messages anonymously to that group
- Confirm destination group after sending
- Allow admins to trace anonymous messages
"""

import asyncpg
import logging
from telethon import events
from telethon.tl.types import Message

logger = logging.getLogger(__name__)

# In-memory cache (speed)
USER_SESSIONS = {}   # user_id -> group_id


class AnonymousMessaging:
    def __init__(self, client, database_url: str):
        self.client = client
        self.database_url = database_url
        self.pool = None

    # -------------------- DB --------------------

    async def connect_db(self):
        self.pool = await asyncpg.create_pool(self.database_url)
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS anon_connections (
                    user_id BIGINT PRIMARY KEY,
                    group_id BIGINT NOT NULL
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS anon_logs (
                    group_id BIGINT,
                    message_id BIGINT,
                    user_id BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, message_id)
                );
            """)

    # -------------------- LINKING --------------------

    async def link_user(self, user_id: int, group_id: int):
        USER_SESSIONS[user_id] = group_id
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO anon_connections (user_id, group_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id)
                DO UPDATE SET group_id = $2
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

    # -------------------- SENDING --------------------

    async def send_anonymous(self, user_id: int, text: str):
        group_id = await self.get_linked_group(user_id)
        if not group_id:
            return False, "❌ You are not connected to any group."

        try:
            group = await self.client.get_entity(group_id)

            sent = await self.client.send_message(
                group_id,
                f"🕶 <b>Anonymous</b>:\n{text}",
                parse_mode="html"
            )

            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO anon_logs (group_id, message_id, user_id)
                    VALUES ($1, $2, $3)
                """, group_id, sent.id, user_id)

            return True, f"✅ Sent anonymously to **{group.title}**"

        except Exception as e:
            logger.exception("Anonymous send failed")
            return False, "❌ Failed to send anonymous message."

    # -------------------- TRACE --------------------

    async def trace_message(self, group_id: int, message_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT user_id FROM anon_logs
                WHERE group_id = $1 AND message_id = $2
            """, group_id, message_id)

        return row["user_id"] if row else None

    # -------------------- HANDLERS --------------------

    def register_handlers(self):

        # DM text handler
        @self.client.on(events.NewMessage(incoming=True, private=True))
        async def anon_dm_handler(event):
            if event.text.startswith("/"):
                return

            success, reply = await self.send_anonymous(
                event.sender_id,
                event.text
            )
            await event.reply(reply)

        # /start deep-link handler
        @self.client.on(events.NewMessage(pattern=r"^/start (\-?\d+)$"))
        async def start_handler(event):
            if not event.is_private:
                return

            group_id = int(event.pattern_match.group(1))
            await self.link_user(event.sender_id, group_id)

            await event.reply(
                "🕶 **Anonymous Messaging Enabled**\n\n"
                "Messages you send here will be forwarded anonymously.\n"
                "Admins can trace abuse.\n\n"
                "📌 Tip: Pin this chat for quick access."
            )

        # /trace (admins only – permission check should be external)
        @self.client.on(events.NewMessage(pattern="^/trace$"))
        async def trace_handler(event):
            if not event.reply_to_msg_id:
                return

            sender = await self.trace_message(
                event.chat_id,
                event.reply_to_msg_id
            )

            if sender:
                await event.reply(
                    f"🕵️ [User Profile](tg://user?id={sender})",
                    parse_mode="md"
                )
