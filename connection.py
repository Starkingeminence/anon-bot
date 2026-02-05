# connection.py
import os
import asyncpg
import asyncio

class Database:
    def __init__(self, database_url: str = None):
        # Use DATABASE_URL from env if not explicitly provided
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise ValueError("No DATABASE_URL provided!")
        self.pool: asyncpg.pool.Pool | None = None

    async def connect(self):
        # AsyncPG automatically detects SSL mode if `?sslmode=require` is in the URL
        try:
            self.pool = await asyncpg.create_pool(self.database_url)
            print("Database connected ✅")
        except Exception as e:
            print(f"Database connection failed ❌: {e}")

    async def fetch(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("Database not connected")
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("Database not connected")
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

# Global instance for easy import
db = Database()
