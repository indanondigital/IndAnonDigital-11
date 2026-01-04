import os
import asyncpg
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("❌ FATAL ERROR: DATABASE_URL is missing!")
            return

        try:
            # We use a connection pool for PostgreSQL (asyncpg)
            self.pool = await asyncpg.create_pool(dsn=db_url)
            print("✅ Database Connected Successfully")
            await self.create_tables()
            await self.migrate_tables()
        except Exception as e:
            print(f"❌ Database Connection Failed: {e}")

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            # 1. Users Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    gender TEXT,
                    country TEXT,
                    age INT,
                    is_premium BOOLEAN DEFAULT FALSE,
                    vip_expiry TIMESTAMP,
                    current_order_id TEXT 
                );
            """)
            # 2. Search Queue
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS search_queue (
                    user_id BIGINT PRIMARY KEY,
                    looking_for TEXT
                );
            """)
            # 3. Active Chats
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_chats (
                    user_1 BIGINT,
                    user_2 BIGINT
                );
            """)
            # 4. Banned Users
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id BIGINT PRIMARY KEY
                );
            """)
            print("✅ Tables Verified/Created")

    async def migrate_tables(self):
        """Ensures new columns exist if you are updating an old DB"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS age INT;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_expiry TIMESTAMP;")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_order_id TEXT;")
                print("✅ Database Schema Updated")
            except Exception as e:
                print(f"⚠️ Migration Note: {e}")

    # --- USER MANAGEMENT ---
    async def add_user(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)

    async def get_user(self, user_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

    # --- SETTERS ---
    async def set_gender(self, user_id, gender):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET gender = $1 WHERE user_id = $2", gender, user_id)

    async def set_country(self, user_id, country):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET country = $1 WHERE user_id = $2", country, user_id)

    async def set_age(self, user_id, age):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET age = $1 WHERE user_id = $2", age, user_id)

    # --- PAYMENT & VIP LOGIC ---
    async def set_order_id(self, user_id, order_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET current_order_id = $1 WHERE user_id = $2", order_id, user_id)

    async def get_user_by_order_id(self, order_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM users WHERE current_order_id = $1", order_id)
            return row['user_id'] if row else None

    async def check_premium(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_premium, vip_expiry FROM users WHERE user_id = $1", user_id)
            if not row: return False
            if row['is_premium']: return True
            if row['vip_expiry'] and row['vip_expiry'] > datetime.now():
                return True
            return False

    async def make_premium(self, user_id, days=30):
        async with self.pool.acquire() as conn:
            expiry_date = datetime.now() + timedelta(days=days)
            # Sets expiry AND clears the Order ID
            await conn.execute("""
                INSERT INTO users (user_id, vip_expiry, current_order_id) VALUES ($1, $2, NULL)
                ON CONFLICT (user_id) DO UPDATE SET vip_expiry = $2, current_order_id = NULL
            """, user_id, expiry_date)
            print(f"✅ DB: User {user_id} VIP extended by {days} days.")

    # --- CHAT & BAN LOGIC ---
    async def ban_user(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM search_queue WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM active_chats WHERE user_1 = $1 OR user_2 = $1", user_id)

    async def unban_user(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM banned_users WHERE user_id = $1", user_id)

    async def is_banned(self, user_id):
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1 FROM banned_users WHERE user_id = $1", user_id)
            return val is not None

    async def add_to_queue(self, user_id, looking_for):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO search_queue (user_id, looking_for) 
                VALUES ($1, $2) 
                ON CONFLICT (user_id) DO UPDATE SET looking_for = $2
            """, user_id, looking_for)

    async def remove_from_queue(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM search_queue WHERE user_id = $1", user_id)

    async def find_match(self, user_id, looking_for):
        async with self.pool.acquire() as conn:
            query = "SELECT user_id, looking_for FROM search_queue WHERE user_id != $1"
            params = [user_id]
            
            if looking_for != 'any':
                query += " AND user_id IN (SELECT user_id FROM users WHERE gender = $2)"
                params.append(looking_for)
            
            # Lock the row to prevent double-matching
            query += " LIMIT 1 FOR UPDATE SKIP LOCKED"
            
            row = await conn.fetchrow(query, *params)
            
            if row:
                partner_id = row['user_id']
                await conn.execute("INSERT INTO active_chats (user_1, user_2) VALUES ($1, $2)", user_id, partner_id)
                await conn.execute("DELETE FROM search_queue WHERE user_id = $1 OR user_id = $2", user_id, partner_id)
                return partner_id
            return None

    async def get_partner(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM active_chats WHERE user_1 = $1 OR user_2 = $1", user_id)
            if row:
                return row['user_2'] if row['user_1'] == user_id else row['user_1']
            return None

    async def disconnect(self, user_id):
        async with self.pool.acquire() as conn:
            partner = await self.get_partner(user_id)
            if partner:
                await conn.execute("DELETE FROM active_chats WHERE user_1 = $1 OR user_2 = $1 OR user_1 = $2 OR user_2 = $2", user_id, partner)
            return partner

    async def is_searching(self, user_id):
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1 FROM search_queue WHERE user_id = $1", user_id)
            return val is not None

    # --- 🆕 NEW VIP RE-CHAT FEATURE (SQL Version) ---
    async def connect_users(self, user1_id, user2_id):
        """Forces two specific users to match."""
        async with self.pool.acquire() as conn:
            # 1. Remove both from queue (if they are searching)
            await conn.execute("DELETE FROM search_queue WHERE user_id = $1 OR user_id = $2", user1_id, user2_id)
            
            # 2. Insert into active chats
            await conn.execute("INSERT INTO active_chats (user_1, user_2) VALUES ($1, $2)", user1_id, user2_id)
            return True

db = Database()