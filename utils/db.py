import aiosqlite
import json

DB_PATH = "bot.db"

async def setup():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER,
            key TEXT,
            value TEXT,
            PRIMARY KEY (guild_id, key)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS reaction_roles (
            message_id INTEGER,
            emoji TEXT,
            role_id INTEGER,
            PRIMARY KEY (message_id, emoji)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_levels (
            guild_id INTEGER,
            user_id INTEGER,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            total_messages INTEGER DEFAULT 0,
            voice_minutes INTEGER DEFAULT 0,
            total_earned INTEGER DEFAULT 0,
            last_message_ts REAL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS level_roles (
            guild_id INTEGER,
            level INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, level)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS level_settings (
            guild_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            channel_id INTEGER DEFAULT 0,
            dm_notifications INTEGER DEFAULT 0,
            text_min_coins INTEGER DEFAULT 8,
            text_max_coins INTEGER DEFAULT 15,
            text_xp INTEGER DEFAULT 20,
            vc_coins_per_min INTEGER DEFAULT 12,
            vc_xp_per_min INTEGER DEFAULT 18
        )''')
        await db.commit()

async def get_setting(guild_id: int, key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?', (guild_id, key)) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except:
                    return row[0]
            return default

async def set_setting(guild_id: int, key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        val_str = json.dumps(value)
        await db.execute('INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)', (guild_id, key, val_str))
        await db.commit()

async def add_mod_case(guild_id: int, case_type: str, user_id: int, moderator_id: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS mod_cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            case_type TEXT,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Get the next case number for this guild
        async with db.execute("SELECT COUNT(*) FROM mod_cases WHERE guild_id = ?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
            guild_case_num = (row[0] if row else 0) + 1
            
        await db.execute('''INSERT INTO mod_cases 
            (guild_id, case_type, user_id, moderator_id, reason) 
            VALUES (?, ?, ?, ?, ?)''', 
            (guild_id, case_type, user_id, moderator_id, reason))
        await db.commit()
        return guild_case_num
