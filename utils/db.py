import aiosqlite
import json
import sqlite3

DB_PATH = "bot.db"

# Modules that stay OFF until an administrator explicitly turns them on.
# This is the single source of truth for every module's default state; both the
# bot (bot.is_module_enabled) and the web dashboard read it, so they can never
# disagree about whether a disabled module should be running.
DEFAULT_DISABLED_MODULES = {"welcome", "giveaway", "colors", "tempvoice"}


def module_default_enabled(module_name: str) -> bool:
    """Default enabled state for a module when the guild has no saved value."""
    return module_name not in DEFAULT_DISABLED_MODULES


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with a busy timeout so concurrent reads/writes
    from the bot and the web dashboard wait instead of failing with
    'database is locked'."""
    conn = sqlite3.connect(path or DB_PATH, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

async def setup():
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL lets the web dashboard write while the bot reads, instead of both
        # hitting 'database is locked' (which used to silently drop toggles).
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=15000")
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

        await db.execute('''CREATE TABLE IF NOT EXISTS music_247 (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            enabled INTEGER DEFAULT 0
        )''')

        # One-time migration: make the newly default-OFF modules actually OFF for
        # guilds that still have an old "enabled" value saved. Runs exactly once,
        # so an administrator can still turn them back on afterwards and have it
        # stick.
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'modules_default_off_v1'"
        ) as cur:
            _migrated = await cur.fetchone()
        if not _migrated:
            await db.execute(
                "DELETE FROM guild_modules WHERE module_name IN ('giveaway', 'colors', 'tempvoice')"
            )
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (0, 'modules_default_off_v1', '1')"
            )

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
    """Synchronous read for Flask web endpoints.

    Raises on failure so callers can retry; silently returning {} would make a
    module fall back to its default and appear to "turn itself back on".
    """
    modules = {}
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('SELECT module_name, enabled FROM guild_modules WHERE guild_id = ?', (guild_id,))
        for row in cur.fetchall():
            modules[row[0]] = bool(row[1])
    finally:
        conn.close()
    return modules

def set_guild_module_sync(guild_id: int, module_name: str, enabled: bool) -> bool:
    """Synchronous write for Flask web endpoints. Returns True on success."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('''INSERT INTO guild_modules (guild_id, module_name, enabled, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, module_name) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP''',
            (guild_id, module_name, 1 if enabled else 0))
        conn.commit()
        return True
    except Exception as e:
        print(f"[MODULES] ❌ Failed to save {module_name}={enabled} for guild {guild_id}: {e}", flush=True)
        return False
    finally:
        conn.close()

async def get_247_setting(guild_id: int) -> tuple[bool, int]:
    """Returns (enabled, channel_id) for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT enabled, channel_id FROM music_247 WHERE guild_id = ?', (guild_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return bool(row[0]), int(row[1] or 0)
    return False, 0

async def set_247_setting(guild_id: int, channel_id: int, enabled: bool):
    """Save 24/7 setting for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO music_247 (guild_id, channel_id, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                enabled = excluded.enabled''',
            (guild_id, channel_id, 1 if enabled else 0))
        await db.commit()

async def get_all_247_guilds() -> list[tuple[int, int]]:
    """Returns list of (guild_id, channel_id) where 24/7 mode is enabled."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT guild_id, channel_id FROM music_247 WHERE enabled = 1') as cur:
            rows = await cur.fetchall()
            return [(int(r[0]), int(r[1] or 0)) for r in rows]

