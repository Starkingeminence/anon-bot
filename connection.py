import asyncpg
import asyncio

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self, database_url: str):
        """
        Connect to the PostgreSQL database using asyncpg.
        Supports SSL for Supabase.
        """
        try:
            self.pool = await asyncpg.create_pool(
                dsn=database_url,
                min_size=1,
                max_size=10,
                ssl=True  # Supabase requires SSL
            )
            print("Database connected ✅")
        except Exception as e:
            print("Database connection failed ❌:", e)
            # Optional: retry logic
            # await asyncio.sleep(5)
            # await self.connect(database_url)

    async def fetch(self, query: str, *args):
        """Fetch multiple rows."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        """Fetch a single row."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def execute(self, query: str, *args):
        """Execute a query (INSERT, UPDATE, DELETE)."""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def close(self):
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            print("Database connection closed ✅")
