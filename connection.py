import asyncpg
import logging
import os
import socket

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self, database_url: str | None = None):
        """
        Connect to PostgreSQL using asyncpg.
        Designed for Supabase / hosted Postgres.
        Forces IPv4 to avoid Render IPv6 routing issues.
        """

        url = database_url or os.getenv("DATABASE_URL")

        if not url:
            raise ValueError("DATABASE_URL environment variable is missing")

        try:
            self.pool = await asyncpg.create_pool(
                dsn=url,
                ssl="require",              # Supabase requires SSL
                family=socket.AF_INET,      # Force IPv4 (fixes Render issue)
                min_size=1,
                max_size=10,
                command_timeout=60,
            )

            # Test connection immediately
            async with self.pool.acquire() as conn:
                await conn.execute("SELECT 1")

            logger.info("Database connected successfully ✅")

        except Exception as e:
            logger.exception("Database connection failed ❌")
            raise

    async def execute(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("Database connection closed ✅")


# Singleton instance
db = Database()
