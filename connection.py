import os
import asyncpg

class Database:
    def __init__(self):
        self.pool = None
        self.url = os.getenv("DATABASE_URL")

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(self.url)
            print("Database connected ✅")
        except Exception as e:
            print("Database connection failed ❌:", e)

# This creates a single global instance, so main.py can import it
db = Database()
