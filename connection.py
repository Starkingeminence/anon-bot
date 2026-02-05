import asyncpg
import logging
import os

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self, database_url=None):
        """
        Connect to the PostgreSQL database using asyncpg.
        Supports SSL mode for Supabase/PostgreSQL.
        """
        url = database_url or os.getenv("DATABASE_URL")
        if not url:
            raise ValueError("DATABASE_URL must be provided")

        try:
            # Ensure SSL is required (Supabase requires sslmode=require)
            self.pool = await asyncpg.create_pool(dsn=url, ssl=True)
            logger.info("Database connected successfully ✅")
        except Exception as e:
            logger.error(f"Database connection failed ❌: {e}")
            raise

    async def execute(self, query, *args):
        """
        Execute a query that doesn't return results (INSERT, UPDATE, DELETE)
        """
        async with self.pool.acquire() as connection:
            await connection.execute(query, *args)

    async def fetch(self, query, *args):
        """
        Fetch multiple rows from a SELECT query
        """
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query, *args):
        """
        Fetch a single row from a SELECT query
        """
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def close(self):
        """
        Close the connection pool
        """
        if self.pool:
            await self.pool.close()
            logger.info("Database connection closed ✅")

# Singleton database instance
db = Database()
