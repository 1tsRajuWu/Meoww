import aiosqlite
import json
import sqlite3

from utils import features, levels

DB_PATH = "bot.db"

# Module defaults now live in the feature registry (utils/features.py) so the
# bot, the dashboard and the help menu can never disagree about which modules
# start on. Kept as a module attribute for backwards compatibility.
DEFAULT_DISABLED_MODULES = set(features.DEFAULT_DISABLED_MODULES)


def module_default_enabled(module_name: str) -> bool:
    """Default enabled state for a module when the guild has no saved value."""
    return features.default_enabled(module_name)


async def ensure_columns(db, table: str, columns: dict[str, str]) -> list[str]:
    """Add any missing columns to an existing table, idempotently.

    ``CREATE TABLE IF NOT EXISTS`` never changes a table that already exists, so
    a live database (and every cloud-restored copy of it) would silently keep the
    old shape when a feature adds a column. ``ALTER TABLE ... ADD COLUMN`` is
    additive and safe to repeat, which is what makes upgrades work without
    touching stored rows. ``table`` and the column names come from this file, not
    from user input.
    """
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    added: list[str] = []
    for name, declaration in columns.items():
        if name in existing:
            continue
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        added.append(name)
    if added:
        print(f"[DB] Added column(s) {', '.join(added)} to {table}.", flush=True)
    return added


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with a busy timeout so concurrent reads/writes
    from the bot and the web dashboard wait instead of failing with
    'database is locked'."""
    conn = sqlite3.connect(path or DB_PATH, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def run_atomic(work):
    """Run ``work(cursor)`` inside a single ``BEGIN IMMEDIATE`` transaction.

    Any balance move must go through this helper. The old code hand-wrote the
    same "begin / conditional update / check rowcount / commit" dance in nine
    places, and every future money command had to remember to copy it or it
    silently reintroduced the duplicate-coin race.

    ``work`` receives a cursor so it can branch on ``cursor.rowcount`` (for
    example, only crediting the receiver when the debit actually matched) and
    its return value is passed back to the caller. Any exception rolls the
    whole transaction back and re-raises.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            result = work(cursor)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

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
        # Timed shop perks (XP/coin multipliers, rob shields, luck). One row per
        # member per effect, so re-buying extends the clock instead of stacking
        # rows. Created here as well as in ``CREATE TABLE IF NOT EXISTS`` style so
        # a database restored from a backup taken before this table existed gets
        # it on the next boot.
        await db.execute('''CREATE TABLE IF NOT EXISTS item_effects (
            user_id INTEGER,
            guild_id INTEGER,
            effect TEXT,
            value REAL DEFAULT 0,
            expires_ts REAL DEFAULT 0,
            PRIMARY KEY (user_id, guild_id, effect)
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

        await db.execute('''CREATE TABLE IF NOT EXISTS loans (
            user_id INTEGER,
            guild_id INTEGER,
            amount INTEGER,
            repay_amount INTEGER,
            due_ts REAL,
            plan TEXT DEFAULT 'standard',
            status TEXT DEFAULT 'active',
            taken_ts REAL DEFAULT 0,
            late_fee_applied INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, guild_id)
        )''')

        # Loans taken before the plans/penalties existed have no plan or
        # timestamp, and the term was never enforced. Backfill so an old loan
        # behaves like a standard one taken at the moment it was written.
        await ensure_columns(db, "loans", {
            "plan": "TEXT DEFAULT 'standard'",
            "status": "TEXT DEFAULT 'active'",
            "taken_ts": "REAL DEFAULT 0",
            "late_fee_applied": "INTEGER DEFAULT 0",
        })
        await db.execute(
            "UPDATE loans SET plan='standard' WHERE plan IS NULL OR plan=''"
        )
        await db.execute(
            "UPDATE loans SET status='active' WHERE status IS NULL OR status=''"
        )

        # The market. Positions and fills are per member per guild like the rest
        # of the economy; ``market_quotes`` is the last board that was fetched, so
        # a restart shows real prices (with their age) instead of an empty market
        # — trading still refuses anything older than ``market.MAX_QUOTE_AGE_SECONDS``.
        await db.execute('''CREATE TABLE IF NOT EXISTS market_holdings (
            user_id INTEGER,
            guild_id INTEGER,
            symbol TEXT,
            quantity REAL DEFAULT 0,
            cost_coins REAL DEFAULT 0,
            PRIMARY KEY (user_id, guild_id, symbol)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS market_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            guild_id INTEGER,
            symbol TEXT,
            side TEXT,
            quantity REAL DEFAULT 0,
            unit_price_coins REAL DEFAULT 0,
            coins INTEGER DEFAULT 0,
            realized INTEGER DEFAULT 0,
            ts REAL DEFAULT 0
        )''')
        # Every observation the bot makes, so charts have a real series even for
        # instruments whose source publishes no chart endpoint. Pruned on write.
        await db.execute('''CREATE TABLE IF NOT EXISTS market_history (
            symbol TEXT,
            ts REAL,
            price_usd REAL,
            source TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS market_quotes (
            symbol TEXT PRIMARY KEY,
            price_usd REAL DEFAULT 0,
            change_pct REAL,
            source TEXT,
            as_of REAL DEFAULT 0
        )''')

        # One-time migration for the 1:1 rate change (250 coins per dollar became
        # 1). Every stored market amount is in coin units, so without this every
        # existing holder's cost basis would be 250x the value of their position —
        # a 99.6% paper loss on a position that never moved. The fills are rescaled
        # too, so `/market history` and the rolling daily buyback ceiling stay in
        # the current unit. Guarded by a marker, so it runs exactly once even
        # across restores of an older database.
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'market_rate_1to1_v1'"
        ) as cur:
            _market_rescaled = await cur.fetchone()
        if not _market_rescaled:
            from utils import market  # local: utils.market imports utils.db

            factor = market.rate_rescale_factor()
            if factor != 1.0:
                await db.execute(
                    "UPDATE market_holdings SET cost_coins = cost_coins * ?", (factor,)
                )
                await db.execute(
                    "UPDATE market_trades SET unit_price_coins = unit_price_coins * ?, "
                    "coins = CAST(ROUND(coins * ?) AS INTEGER), "
                    "realized = CAST(ROUND(realized * ?) AS INTEGER)",
                    (factor, factor, factor),
                )
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) "
                "VALUES (0, 'market_rate_1to1_v1', '1')"
            )

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

        # One-time integrity repair: earlier builds let /setlevel and /givexp
        # write unbounded XP (a 2.4-billion-XP "Level 1351" record is what broke
        # the leaderboard and the rank card). Re-derive every row's level from
        # its XP and clamp anything past the cap so stored data matches the
        # formula the bot actually uses. Runs exactly once.
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'levels_repaired_v1'"
        ) as cur:
            _levels_repaired = await cur.fetchone()
        if not _levels_repaired:
            fixed = 0
            async with db.execute("SELECT guild_id, user_id, xp, level FROM user_levels") as cur:
                rows = await cur.fetchall()
            for guild_id, user_id, row_xp, row_level in rows:
                good_xp = levels.clamp_xp(row_xp or 0)
                good_level, _, _ = levels.calculate_level_from_xp(good_xp)
                if good_xp != (row_xp or 0) or good_level != (row_level or 0):
                    await db.execute(
                        "UPDATE user_levels SET xp=?, level=? WHERE guild_id=? AND user_id=?",
                        (good_xp, good_level, guild_id, user_id),
                    )
                    fixed += 1
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (0, 'levels_repaired_v1', '1')"
            )
            if fixed:
                print(f"[LEVELS] Repaired {fixed} out-of-range level record(s).", flush=True)

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
                except (json.JSONDecodeError, TypeError):
                    # Stored raw (pre-JSON rows) — hand it back as-is instead of raising.
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
        
        # The case number shown to users must be the row's real id. It used to be
        # `COUNT(*) + 1`, which two simultaneous cases could read identically and
        # which drifted from the stored `case_id` as soon as any row was deleted —
        # "Case #3" could point at case_id 57. `lastrowid` is the inserted row's
        # own primary key, so the label always matches the record.
        cursor = await db.execute('''INSERT INTO mod_cases
            (guild_id, case_type, user_id, moderator_id, reason)
            VALUES (?, ?, ?, ?, ?)''',
            (guild_id, case_type, user_id, moderator_id, reason))
        await db.commit()
        return cursor.lastrowid




def read_guild_modules_sync(guild_id: int) -> dict:
    """Synchronous read of every *saved* module row for a guild.

    This is the only reader used by the app — both the asyncio bot path and the
    Flask thread go through it (see ``utils.modules``), so there is a single
    SQLite access pattern instead of two subtly different ones.

    Raises on failure so callers can distinguish "no saved value" from "could
    not read"; silently returning {} would make a module fall back to its
    default and appear to "turn itself back on".
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

def write_guild_module_sync(guild_id: int, module_name: str, enabled: bool) -> bool:
    """Synchronous write of one module toggle. Returns True on success.

    This is the only writer in the codebase. The asyncio path in
    ``utils.modules`` and the Flask endpoints both call it, so a toggle cannot
    be persisted two different ways.
    """
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

