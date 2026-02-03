import asyncpg
from config import DATABASE_URL


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=5
            )

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None


# Singleton DB instance
db = Database()
