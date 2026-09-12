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
        await db.execute('''CREATE TABLE IF NOT EXISTS mod_cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            case_type TEXT,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER,
            guild_id INTEGER,
            wallet INTEGER DEFAULT 0,
            bank INTEGER DEFAULT 0,
            last_daily TEXT,
            last_weekly TEXT,
            last_work TEXT,
            PRIMARY KEY (user_id, guild_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            item_name TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS shop (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            item_name TEXT,
            price INTEGER,
            role_id INTEGER
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
            text_min_coins INTEGER DEFAULT 4,
            text_max_coins INTEGER DEFAULT 10,
            text_xp INTEGER DEFAULT 15,
            vc_coins_per_min INTEGER DEFAULT 2,
            vc_xp_per_min INTEGER DEFAULT 3
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS custom_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            quote TEXT,
            author TEXT,
            source TEXT,
            category TEXT DEFAULT 'funny',
            created_at TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS user_animals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            animal_name TEXT,
            emoji TEXT,
            tier TEXT,
            count INTEGER DEFAULT 1,
            UNIQUE(guild_id, user_id, animal_name)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS marriages (
            guild_id INTEGER,
            user1_id INTEGER,
            user2_id INTEGER,
            married_at TEXT,
            PRIMARY KEY (guild_id, user1_id)
        )''')

        # Giveaway tables
        await db.execute('''CREATE TABLE IF NOT EXISTS giveaways (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            guild_id INTEGER,
            prize TEXT,
            winners INTEGER,
            end_ts REAL,
            host_id INTEGER,
            ended INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS giveaway_entries (
            message_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (message_id, user_id)
        )''')

        # TempVoice (Join-to-Create) tables
        await db.execute('''CREATE TABLE IF NOT EXISTS tempvoice_settings (
            guild_id INTEGER PRIMARY KEY,
            generator_channel_id INTEGER,
            category_id INTEGER
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS tempvoice_channels (
            channel_id INTEGER PRIMARY KEY,
            owner_id INTEGER,
            guild_id INTEGER
        )''')

        # Starboard tables
        await db.execute('''CREATE TABLE IF NOT EXISTS starboard_settings (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            threshold INTEGER DEFAULT 3
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS starboard_posts (
            original_message_id INTEGER PRIMARY KEY,
            starboard_message_id INTEGER,
            guild_id INTEGER,
            stars_count INTEGER DEFAULT 0
        )''')

        await db.execute('''CREATE TABLE IF NOT EXISTS guild_modules (
            guild_id INTEGER,
            module_name TEXT,
            enabled INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, module_name)
        )''')

        # Seed default settings for main guild 1542278896148422676 (Hmm)
        await db.execute('''INSERT OR IGNORE INTO level_settings 
            (guild_id, enabled, channel_id, dm_notifications, text_min_coins, text_max_coins, text_xp, vc_coins_per_min, vc_xp_per_min)
            VALUES (1542278896148422676, 1, 1547637176697757846, 0, 4, 10, 15, 2, 3)''')

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

async def get_guild_modules(guild_id: int) -> dict:
    """Retrieve all module toggle states for a guild asynchronously."""
    modules = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT module_name, enabled FROM guild_modules WHERE guild_id = ?', (guild_id,)) as cursor:
            async for row in cursor:
                modules[row[0]] = bool(row[1])
    return modules

async def set_guild_module(guild_id: int, module_name: str, enabled: bool):
    """Persist a module toggle state asynchronously."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO guild_modules (guild_id, module_name, enabled, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, module_name) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP''',
            (guild_id, module_name, 1 if enabled else 0))
        await db.commit()

def get_guild_modules_sync(guild_id: int) -> dict:
    """Synchronous read for Flask web endpoints."""
    import sqlite3
    modules = {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT module_name, enabled FROM guild_modules WHERE guild_id = ?', (guild_id,))
            for row in cursor.fetchall():
                modules[row[0]] = bool(row[1])
    except Exception as e:
        print(f"Error fetching guild modules sync: {e}")
    return modules

def set_guild_module_sync(guild_id: int, module_name: str, enabled: bool):
    """Synchronous write for Flask web endpoints."""
    import sqlite3
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO guild_modules (guild_id, module_name, enabled, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, module_name) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP''',
                (guild_id, module_name, 1 if enabled else 0))
            conn.commit()
    except Exception as e:
        print(f"Error updating guild module sync: {e}")

