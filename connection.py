import os
import asyncpg

class Database:
    def __init__(self):
        self.pool = None
        self.url = os.getenv("DATABASE_URL")  # <- read from Render env

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(self.url)
            print("Database connected ✅")
        except Exception as e:
            print("Database connection failed ❌:", e)

print("DATABASE_URL:", os.getenv("DATABASE_URL"))
