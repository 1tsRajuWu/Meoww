import aiosqlite
import json
import sqlite3
import time

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


# ── The lockdown ledger's own shape ──────────────────────────────────────────
#
# "Lock every channel" is a bulk write over somebody else's server, and the shape
# that makes it safe is that it remembers what it *changed*: one row per channel
# the lockdown touched, holding the overwrite that was there before. Unlocking
# then restores what the server had rather than blanket-allowing `@everyone` to
# talk. `previous` is the overwrite target as three numbers (allow, deny, and
# whether it existed at all); an empty string means there was nothing there and
# the row should be removed on unlock.
#
# `scope` is who a lock holds: the server-wide command exists so members stop
# talking while administrators and the owner still can, which is only possible if
# the row says which command wrote it. It was added after the table shipped, so
# its statement lives here once and is run by **two** callers — the boot
# (`setup`) and the ledger's own readers and writer — because a database the
# cloud restore swaps in under a running process can predate the column. Without
# that second caller the sweep answered ``no such column: scope`` and read as
# "nothing is locked": a lockdown that silently stops enforcing.
LOCKDOWN_SCHEMA: str = '''CREATE TABLE IF NOT EXISTS lockdown_state (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            previous TEXT NOT NULL DEFAULT '',
            locked_at REAL NOT NULL DEFAULT 0,
            locked_by INTEGER NOT NULL DEFAULT 0,
            scope TEXT NOT NULL DEFAULT 'channel',
            PRIMARY KEY (guild_id, channel_id)
        )'''

#: The columns a restored copy of the ledger may be missing. A row written before
#: the column existed reads as ``channel`` — the strict answer, because a lock
#: that was there before the question is not one that should loosen itself.
LOCKDOWN_COLUMNS: dict[str, str] = {"scope": "TEXT NOT NULL DEFAULT 'channel'"}


async def ensure_lockdown_schema(db) -> list[str]:
    """Make the ledger exist, with every column the code reads, on ``db``.

    Run on the connection that is about to use the ledger rather than trusting
    the boot's ``setup()``: ``utils.cloud_sync`` restores a backup by replacing
    ``bot.db`` with ``os.replace``, so a file the process is using can be older
    than the code reading it. ``CREATE TABLE IF NOT EXISTS`` is a no-op on an
    existing table, which is why the column is added by ``ensure_columns`` as
    well. Idempotent and cheap, so a reader may call it every time.
    """
    await db.execute(LOCKDOWN_SCHEMA)
    return await ensure_columns(db, "lockdown_state", LOCKDOWN_COLUMNS)


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

#: The meetings tables and their indexes, in one place because two callers make
#: them. :func:`setup` creates them on boot, and ``utils.meetings.create``
#: creates them again on the connection it is about to write on — because a live
#: database can be replaced *under* a running process (``utils.cloud_sync``
#: swaps the file in with ``os.replace``), and a boot that already happened is
#: not a promise that the tables are still there. Without this, the write fails
#: with ``no such table: meetings``, which the command can only report as
#: "the database refused the write".
#:
#: Three tables because a meeting is three facts that must not drift: what it is
#: (``meetings``), when somebody should be told about it (``meeting_notices``),
#: and who said they were coming (``meeting_rsvps``).
#:
#: A notice is a row rather than a timer in memory for the same reason a
#: scheduled message is: the promise has to outlive the process, and a restart
#: must not cost somebody their reminder. ``status`` plus ``due_at`` is the
#: queue the cog's sweep drains, and a notice that was never sent keeps its
#: ``note`` saying why — the one question a missed reminder cannot answer from
#: the channel, because it left nothing there.
MEETINGS_SCHEMA: tuple[str, ...] = (
    '''CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            agenda TEXT NOT NULL DEFAULT '',
            -- Unix seconds, UTC. Cards print it as a Discord timestamp, which
            -- each reader's own client renders in their own timezone.
            starts_at INTEGER NOT NULL,
            -- Where the announcement and every notice are posted — not where
            -- the meeting is, which is ``voice_channel_id`` + ``location``.
            channel_id INTEGER NOT NULL,
            voice_channel_id INTEGER,
            location TEXT NOT NULL DEFAULT '',
            -- The role pinged on the announcement and on every notice; NULL is
            -- "nobody is pinged", never @everyone.
            audience_role_id INTEGER,
            -- JSON list of offsets in seconds before ``starts_at``.
            notices TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'scheduled',
            -- Where the announcement card landed, so RSVP buttons and later
            -- edits can find it: a button carries no state of its own.
            announce_message_id INTEGER,
            created_by INTEGER,
            -- The name as it was written on the card, so a rebuilt card after
            -- a redeploy keeps the footer it was posted with.
            created_by_name TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0
        )''',
    'CREATE INDEX IF NOT EXISTS idx_meetings_guild '
    'ON meetings(guild_id, starts_at)',
    '''CREATE TABLE IF NOT EXISTS meeting_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            offset_seconds INTEGER NOT NULL,
            -- pending -> sending -> sent | missed | cancelled. ``sending`` with
            -- a ``claimed_at`` older than the stale window means the process
            -- died mid-send, and the sweep puts it back: a duplicate reminder
            -- is a nuisance, a missing one is the feature not happening.
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT NOT NULL DEFAULT '',
            claimed_at INTEGER,
            sent_at INTEGER
        )''',
    'CREATE INDEX IF NOT EXISTS idx_meeting_notices_due '
    'ON meeting_notices(status, due_at)',
    'CREATE INDEX IF NOT EXISTS idx_meeting_notices_meeting '
    'ON meeting_notices(meeting_id, due_at)',
    '''CREATE TABLE IF NOT EXISTS meeting_rsvps (
            meeting_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            -- yes | maybe | no. One row per person: the last press wins, so
            -- changing your mind is a state rather than a second vote.
            answer TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (meeting_id, user_id)
        )''',
)


#: The one-time claim's own shape: one row per member, ever.
#:
#: ``/claim`` gives a member, once, the floor of coins and level that
#: ``utils/claims`` publishes. The latch is a *row* rather than a column on
#: ``economy`` for two reasons: a member who claims without ever holding coins
#: gets a row here and a wallet that is created by the same transaction, and the
#: primary key is what makes the second claim impossible rather than merely
#: unlikely — two claims arriving in the same instant cannot both insert it.
#: ``money``/``xp``/``level`` are the record of what was handed over, so the
#: grant can be read back and audited instead of recomputed from a ceiling that
#: may since have moved.
#:
#: Written out once, in ``CLAIM_SCHEMA``, because the claim's writer
#: (``utils.claims.claim_once``) is a second caller: a database the cloud restore
#: swaps in under a running process can predate this table, and a boot that
#: already ran is not a promise that it exists.
CLAIM_SCHEMA: tuple[str, ...] = (
    '''CREATE TABLE IF NOT EXISTS claim_grants (
            guild_id INTEGER,
            user_id INTEGER,
            claimed_at TEXT,
            money INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )''',
)


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
            guild_id INTEGER,
            channel_id INTEGER,
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
        # ── The one-time claim's latch ──────────────────────────────────────
        # Created here and again by the claim's own writer — see
        # ``CLAIM_SCHEMA`` for what the row holds and why it is the table that
        # makes a second claim impossible.
        for statement in CLAIM_SCHEMA:
            await db.execute(statement)
        await db.execute(OVERWRITE_SCHEMA)
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
        await db.execute('''CREATE TABLE IF NOT EXISTS bonds (
            guild_id INTEGER,
            user1_id INTEGER,
            user2_id INTEGER,
            kind TEXT,
            since_at TEXT,
            PRIMARY KEY (guild_id, user1_id, kind)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS music_tracks (
            guild_id INTEGER,
            entity_id TEXT,
            kind TEXT,
            url TEXT,
            title TEXT,
            artist TEXT,
            art TEXT,
            added_by INTEGER,
            added_at TEXT,
            PRIMARY KEY (guild_id, entity_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS music_listens (
            guild_id INTEGER,
            entity_id TEXT,
            user_id INTEGER,
            listened_at TEXT,
            PRIMARY KEY (guild_id, entity_id, user_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS music_jam (
            guild_id INTEGER PRIMARY KEY,
            url TEXT,
            set_by INTEGER,
            set_at TEXT,
            channel_id INTEGER,
            fails INTEGER DEFAULT 0,
            last_checked TEXT
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

        # One row per server that activated a licence. The activation itself is
        # counted by Lemon Squeezy against the licence, so this is the bot's
        # record of *which* server spent it — the store is the authority on how
        # many are left.
        await db.execute('''CREATE TABLE IF NOT EXISTS guild_plans (
            guild_id INTEGER PRIMARY KEY,
            plan TEXT,
            license_key TEXT,
            instance_id TEXT,
            license_status TEXT,
            activation_limit INTEGER DEFAULT 0,
            activation_usage INTEGER DEFAULT 0,
            customer_email TEXT,
            activated_by INTEGER,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            checked_at TIMESTAMP
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
        # DaCat Bank: the house account the DaCat Fee is paid into. One row per
        # guild, because a server's coins are its own. Created here as well as
        # defensively in ``utils/meowfee.py``: a database restored from a backup
        # taken before this table existed must be able to start an empty bank
        # rather than failing every transfer.
        await db.execute('''CREATE TABLE IF NOT EXISTS meow_bank (
            guild_id INTEGER PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 0
        )''')

        # The operator's own word about a system, and it is deliberately *not*
        # keyed by guild: this is the fleet-wide override that makes a system
        # off everywhere, including in servers whose administrators switched it
        # on. An absent row is "auto" — the server's own switch decides, which
        # is the behaviour every server had before this table existed, so a
        # database that has never seen it behaves exactly as it did.
        await db.execute('''CREATE TABLE IF NOT EXISTS operator_modules (
            module_name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL
        )''')

        await db.execute('''CREATE TABLE IF NOT EXISTS market_quotes (
            symbol TEXT PRIMARY KEY,
            price_usd REAL DEFAULT 0,
            change_pct REAL,
            source TEXT,
            as_of REAL DEFAULT 0
        )''')

        # The radio's two tables, and they are the *new* names for two old ones.
        # ``radio_stay`` is the seat a server asked the bot to hold: a channel kept
        # through silence and through restarts, which is what makes `/radio` a
        # radio rather than a player. ``radio_last`` is the station a server last
        # put on — one row per server, because the question it answers is "what do
        # we put on when nobody wants to choose" and that has exactly one answer.
        # ``as_of`` is here so a stale choice is visible.
        await db.execute('''CREATE TABLE IF NOT EXISTS radio_stay (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS radio_last (
            guild_id INTEGER PRIMARY KEY,
            station TEXT DEFAULT '',
            as_of REAL DEFAULT 0
        )''')

        # The rename, carried rather than asked for — see the helper for what it
        # does with the old rows and why they are left in place.
        await _carry_radio_rename(db)

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

        # One-time migration: the Market ships switched OFF — the owner asked for
        # it disabled for now. Saved rows are deleted rather than flipped to 0,
        # the same move as the v1 block above: no row means "fall back to the
        # registry's default", and `modules._load_from_db` merges defaults first.
        # Runs exactly once, so an administrator can turn it back on afterwards
        # and have it stick. (The three systems that used to be named here —
        # Starboard, Voice Company and Radio/Q&A — have since been retired, so a
        # saved row for any of them is inert: `modules._load_from_db` ignores an
        # id the registry no longer declares.)
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'modules_default_off_v2'"
        ) as cur:
            _migrated_v2 = await cur.fetchone()
        if not _migrated_v2:
            await db.execute(
                "DELETE FROM guild_modules WHERE module_name = 'market'"
            )
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (0, 'modules_default_off_v2', '1')"
            )

        # One-time migration: Auto Moderation is its own system again. It was
        # merged into Moderation & Logs once, and that merge is why a server
        # could not stop the automatic filters without stopping bans, kicks,
        # warnings and logging too. A server that had Moderation switched off
        # had the filters off with it, so those filters are written off here
        # rather than silently switched back on by the new system's own
        # default. `INSERT OR IGNORE` because a row this server wrote itself is
        # its answer, not this migration's to overwrite. Runs exactly once, so
        # either switch may be changed afterwards and stick.
        # One-time migration: the reaction-roles table grows the two columns the
        # feature needs now that it is back. The retired cog stored a mapping as
        # (message_id, emoji, role_id) with no guild and no channel, so nothing
        # could list it, scope it or write it down as an action; the dashboard
        # manages the same rows, and it cannot answer "whose mapping is this"
        # without a guild. `ALTER TABLE` rather than a rebuild because the rows
        # are the only record of mappings members may still be reacting to, and a
        # dropped table is a promise the bot stops keeping silently. Guarded by
        # the column list rather than a marker, so a database restored from an
        # older backup is repaired too.
        async with db.execute("PRAGMA table_info(reaction_roles)") as cur:
            _reaction_columns = {row[1] for row in await cur.fetchall()}
        if "guild_id" not in _reaction_columns:
            await db.execute("ALTER TABLE reaction_roles ADD COLUMN guild_id INTEGER")
        if "channel_id" not in _reaction_columns:
            await db.execute("ALTER TABLE reaction_roles ADD COLUMN channel_id INTEGER")

        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'modules_automod_split_v1'"
        ) as cur:
            _automod_split = await cur.fetchone()
        if not _automod_split:
            await db.execute(
                "INSERT OR IGNORE INTO guild_modules (guild_id, module_name, enabled, updated_at) "
                "SELECT guild_id, 'automod', 0, CURRENT_TIMESTAMP FROM guild_modules "
                "WHERE module_name = 'moderation' AND enabled = 0"
            )
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) "
                "VALUES (0, 'modules_automod_split_v1', '1')"
            )

        # One-time sweep: saved rows for modules the registry has retired. This
        # pass's example is `music` — the radio rewrite removed that module, and a
        # guild that had it switched on kept a row for a module nothing reads any
        # more: `modules._load_from_db` ignores unknown ids, `set_enabled` refuses
        # to write one, and both the dashboard and /help are built from the
        # registry. So the row is the only place that system still exists, and no
        # switch anywhere can turn it off. (`automod` was the same kind of orphan
        # while it was merged into Moderation & Logs; it is a live system again —
        # see the split migration above — which is exactly why a hand-written list
        # of retired names was the wrong shape: a retirement can be undone.) A
        # list of retired names only holds the retirements that were known when
        # somebody wrote it, so this asks the registry instead.
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'modules_retired_swept_v1'"
        ) as cur:
            _retired_swept = await cur.fetchone()
        if not _retired_swept:
            keep = sorted(features.TOGGLEABLE_IDS)
            await db.execute(
                "DELETE FROM guild_modules WHERE module_name NOT IN "
                f"({', '.join('?' for _ in keep)})",
                keep,
            )
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) "
                "VALUES (0, 'modules_retired_swept_v1', '1')"
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

        # ── Activity Tracker: per-day, per-channel message & voice tracking ──
        # 365-day rolling window with exact counts per channel.
        await db.execute('''CREATE TABLE IF NOT EXISTS activity_messages (
            guild_id INTEGER,
            user_id INTEGER,
            channel_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, channel_id, date)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS activity_voice (
            guild_id INTEGER,
            user_id INTEGER,
            channel_id INTEGER,
            date TEXT,
            seconds INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, channel_id, date)
        )''')
        # Indexes for the most common leaderboard / summary queries
        await db.execute('CREATE INDEX IF NOT EXISTS idx_act_msg_guild_date ON activity_messages(guild_id, date)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_act_vc_guild_date ON activity_voice(guild_id, date)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_act_msg_user ON activity_messages(guild_id, user_id, date)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_act_vc_user ON activity_voice(guild_id, user_id, date)')

        # ── Ticket feedback ──────────────────────────────────────────────────
        # A row is written when a ticket closes (rating still NULL) and filled in
        # by the rating itself, so a rating answered from a DM — long after the
        # ticket channel is gone — still knows who opened and who handled it.
        # ``rating IS NULL`` is also what makes a second rating a no-op instead
        # of a duplicate log post.
        await db.execute('''CREATE TABLE IF NOT EXISTS ticket_feedback (
            ticket_channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            author_id INTEGER,
            claimed_by INTEGER,
            category TEXT,
            rating INTEGER,
            comment TEXT
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_ticket_feedback_guild ON ticket_feedback(guild_id)'
        )

        # ── Scheduled dashboard messages ────────────────────────────────────
        # A message the panel promised to send later has to outlive the process
        # (and the browser), so it is a row rather than a timer in memory. The
        # stored payload is what actually gets posted, so nothing depends on the
        # page that created it still being open when the time comes.
        #
        # ``status`` plus ``run_at`` is the queue the delivery loop drains;
        # ``last_run_at`` is what makes a repeating message show its last outcome
        # while staying pending for its next slot.
        await db.execute('''CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_ids TEXT,
            payload TEXT,
            preview TEXT DEFAULT '',
            run_at INTEGER,
            repeat_minutes INTEGER DEFAULT 0,
            -- Named generator to run when it fires (``utils.digest``), or '' for a
            -- message whose text was typed at queue time.
            generator TEXT DEFAULT '',
            -- 'now' for a message the dashboard posted straight away, kept as the
            -- record of it; 'schedule' for one that waited in this queue.
            source TEXT DEFAULT 'schedule',
            status TEXT DEFAULT 'pending',
            created_by INTEGER,
            created_at INTEGER,
            claimed_at INTEGER,
            fired_at INTEGER,
            last_run_at INTEGER,
            fire_count INTEGER DEFAULT 0,
            fail_streak INTEGER DEFAULT 0,
            last_error TEXT,
            last_results TEXT
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_messages(status, run_at)'
        )
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_sched_guild '
            'ON scheduled_messages(guild_id, status, run_at)'
        )
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_sched_history '
            'ON scheduled_messages(guild_id, last_run_at)'
        )

        # ── Meetings a server schedules, and the notices owed on them ─────
        # Written out once, in ``MEETINGS_SCHEMA``, because the meeting writer
        # makes the same tables itself when it cannot find them.
        for statement in MEETINGS_SCHEMA:
            await db.execute(statement)
        # Access-path indexes for the five tables that grow without one. The
        # composite primary keys cover economy, user_levels, item_effects, loans
        # and market_holdings, but these are scanned today: a chart series reads
        # market_history by symbol and its prune deletes by ts, trade history and
        # the buyback ceiling read market_trades by member, and /inventory,
        # /quote and /warnings each filter on a column that has no index at all.
        # Idempotent and additive, so a restored older database gains them on boot.
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_market_history_symbol_ts "
            "ON market_history(symbol, ts)",
            "CREATE INDEX IF NOT EXISTS idx_market_trades_owner "
            "ON market_trades(guild_id, user_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_market_trades_ts ON market_trades(ts)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_owner ON inventory(guild_id, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_custom_quotes_guild ON custom_quotes(guild_id)",
            "CREATE INDEX IF NOT EXISTS idx_warnings_owner ON warnings(guild_id, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_mod_cases_guild ON mod_cases(guild_id, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_economy_guild ON economy(guild_id)",
            "CREATE INDEX IF NOT EXISTS idx_economy_wealth ON economy(guild_id, wallet, bank)",
        ):
            await db.execute(statement)
        # ``CREATE TABLE IF NOT EXISTS`` never changes a table that already exists,
        # and this table shipped before generators did — so the column is added
        # additively on boot rather than only being declared above.
        await ensure_columns(db, "scheduled_messages", {
            "generator": "TEXT DEFAULT ''",
            "source": "TEXT DEFAULT 'schedule'",
        })
        # Databases restored from legacy schemas might lack guild_id in economy/inventory.
        await ensure_columns(db, "economy", {
            "guild_id": "INTEGER DEFAULT 0",
        })
        await ensure_columns(db, "inventory", {
            "guild_id": "INTEGER DEFAULT 0",
        })
        await ensure_columns(db, "item_effects", {
            "guild_id": "INTEGER DEFAULT 0",
        })

        # ── Signed-in dashboard sessions ────────────────────────────────────
        # A Flask session is a signed cookie and cannot be recalled; this is the
        # row that can. One per sign-in, carrying the scope it was granted and
        # the moment it stops being believed — see ``utils/sessions.py``, which
        # also creates it defensively on first use so a database restored from a
        # backup taken before this existed does not break every sign-in.
        await db.execute('''CREATE TABLE IF NOT EXISTS dashboard_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sid TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL DEFAULT '',
            guild_id INTEGER,
            user_id INTEGER,
            label TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL DEFAULT 0,
            seen_at REAL NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL DEFAULT 0,
            revoked_at REAL
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_sessions_live '
            'ON dashboard_sessions(revoked_at, expires_at)'
        )

        # ── Who read a page no human can reach ──────────────────────────────
        # The anti-AI guard's ledger: one row per identified crawler refusal or
        # trap fetch, folded so a collector that asks four hundred times is one
        # line with a count. Kept here as well as in ``utils/ai_guard.py`` so a
        # database restored from an older backup starts with an empty ledger
        # rather than failing every request that touches one. See that module
        # for why the evidence is kept at all, and for its retention window.
        await db.execute('''CREATE TABLE IF NOT EXISTS net_guard_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            hits INTEGER NOT NULL DEFAULT 1
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_net_guard_at ON net_guard_events(at)'
        )

        # ── What a sign-in agreed to ────────────────────────────────────────
        # The clickwrap's record: which *revision* of the three documents was
        # accepted (a content digest, not a hand-kept number), by which scope or
        # account, from which address and browser, when. It is the one row in
        # this database that answers a question asked after an account is gone,
        # which is why its retention window is the longest here.
        await db.execute('''CREATE TABLE IF NOT EXISTS legal_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL DEFAULT 0,
            version TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            guild_id INTEGER,
            user_id INTEGER,
            label TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT ''
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_legal_acceptances_at '
            'ON legal_acceptances(at)'
        )

        # A server's own name and picture for the bot. Discord keeps these
        # per guild (they are fields on the bot's *member*, not on the
        # application), so this table is the dashboard's record of what was
        # asked for — the live values at Discord are the authority in between,
        # and they survive a restart on their own.
        await db.execute('''CREATE TABLE IF NOT EXISTS guild_identity (
            guild_id INTEGER PRIMARY KEY,
            nick TEXT,
            bio TEXT,
            avatar BLOB,
            banner BLOB,
            updated_at TEXT,
            updated_by INTEGER
        )''')

        # ── A lockdown's own undo, and why it needs one ──────────────────────
        # One definition, run on boot here and again by the ledger's own readers
        # and writer — see ``LOCKDOWN_SCHEMA`` for what the table holds and why
        # the second caller exists.
        await ensure_lockdown_schema(db)

        # ── One locked voice channel, and everything needed to give it back ───
        # The same shape as the lockdown ledger above, one room at a time, for the
        # same reason: a lock writes somebody else's permissions, so the row holds
        # what was there *before* — the `@everyone` overwrite as `allow:deny` (or
        # `''` for "there was none"), the previous `user_limit`, and the previous
        # channel name — and releasing restores exactly that rather than
        # blanket-allowing a room the server had locked for its own reasons.
        #
        # `holder_id` is 0 for a **quiet-hours** lock, which is a room nobody
        # holds; that reads as "no holder" everywhere rather than as a member id
        # 0, and it is also what stops the holder-left release rule from firing on
        # a room that never had one. `keep` is the per-lock override of the
        # server's own default, `turned_away` is the count the status card shows,
        # and `guests`/`guest_roles` are comma-separated ids because the list is
        # read as a whole and never queried by member.
        await db.execute('''CREATE TABLE IF NOT EXISTS alone_time (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            holder_id INTEGER NOT NULL DEFAULT 0,
            holder_name TEXT NOT NULL DEFAULT '',
            guests TEXT NOT NULL DEFAULT '',
            guest_roles TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            previous TEXT NOT NULL DEFAULT '',
            previous_limit INTEGER NOT NULL DEFAULT 0,
            previous_name TEXT NOT NULL DEFAULT '',
            locked_as TEXT NOT NULL DEFAULT '',
            keep INTEGER NOT NULL DEFAULT 0,
            turned_away INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )''')

        # The room's name while it is locked is a column this table gained after
        # it was first written, and ``CREATE TABLE IF NOT EXISTS`` never alters
        # an existing one — so a live database (and every cloud-restored copy of
        # it) would keep the old shape and a release would compare against a
        # name it never set. ``ensure_columns`` is the same idempotent upgrade
        # the loans and support tables use.
        await ensure_columns(db, "alone_time", {
            "locked_as": "TEXT NOT NULL DEFAULT ''",
        })

        # The holder's private reason used to be stored in `note`, and it is one
        # of the two things this feature now promises never to write down (see
        # `utils/alonetime.py`): it lives in that process's memory for the life of
        # the lock and nowhere else. The column stays — dropping a column on a
        # live database is a migration with no upside, and old rows still parse —
        # but every boot blanks it, so a lock taken before this change stops
        # holding somebody's words the next time the bot starts. Nothing writes a
        # non-empty value into it any more: `save_alone_time` does not even take a
        # note, which is the shape that makes the promise hold rather than a
        # convention someone could forget.
        await db.execute("UPDATE alone_time SET note = '' WHERE note <> ''")

        # ── Servers the bot refuses to be in ─────────────────────────────────
        # The operator's own decision, which has no Discord equivalent: leaving a
        # server is `/kick` applied to the bot, and *staying* left is a list.
        # Enforced on join (`on_guild_join` leaves immediately) rather than only
        # when somebody clicks leave, because a blocklist that a re-invite can
        # walk around is a note to self rather than a rule.
        await db.execute('''CREATE TABLE IF NOT EXISTS guild_blocklist (
            guild_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            added_at REAL NOT NULL DEFAULT 0,
            added_by TEXT NOT NULL DEFAULT ''
        )''')

        # ── What was done through the website ───────────────────────────────
        # Discord's own audit log credits the **bot**, because the bot is what
        # performed the action — so a panel action has no audit entry naming the
        # person who asked for it. This is that record, and it is not a
        # convenience: it is the only place a web-signed-in administrator's
        # identity is attached to what happened. Same three rules as the
        # moderation record: the action and its outcome, never the text somebody
        # typed into anything else.
        await db.execute('''CREATE TABLE IF NOT EXISTS web_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL DEFAULT 0,
            scope TEXT NOT NULL DEFAULT '',
            actor_id INTEGER NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            guild_id INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT ''
        )''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_web_actions_at ON web_actions(at)'
        )
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_web_actions_guild ON web_actions(guild_id, at)'
        )

        # ── The anonymous question box ───────────────────────────────────────
        # **Retired 2026-10-06, and any table that already exists is left
        # alone.** ``qna_questions`` holds every question ever asked with the
        # asker's name beside it, and nothing reads it now — but dropping it
        # would destroy the only copy of a record somebody was promised is kept,
        # for a feature that may come back. Nothing creates it any more: it was
        # ``utils/qna.CREATE_TABLE`` and that module is in
        # ``deleted_features/``, so the schema returns with it.

        # ── The support bridge ────────────────────────────────────────────────
        # The conversation an operator holds from the web: every message posted
        # into the support channel from a browser, and every reply taken in from
        # it. Two things about this table are load-bearing rather than storage —
        # the unique index over Discord's message id, which is what lets the
        # listener and an on-demand pull both run over the same message without
        # the conversation growing a duplicate line, and the timestamp taken
        # from Discord's own ``created_at`` rather than from the clock here, so a
        # pulled message lands where it belongs in the conversation. Both are
        # defined beside the rest of the rules in ``utils/support`` and imported
        # here rather than copied, for the reason the reaction-role and level
        # tables are.
        from utils import support as _support  # noqa: PLC0415

        await db.execute(_support.CREATE_TABLE)
        await db.execute(_support.CREATE_INDEX)

        # One-time repair of the fixtures that reached production — last, because
        # it is the only migration here that deletes rows from a table this
        # function also creates. This is the other half of what
        # ``tests/__init__.py`` describes: ``test_scheduler`` inserted 3,200 rows
        # through the *default* database path, the bot running on that same
        # checkout uploaded the result as the backup, and the deploy restores
        # from it — so the live database carries three thousand queued messages
        # for a server that does not exist, and has carried them through every
        # restore since.
        #
        # They are invisible in Discord (the delivery loop works one guild at a
        # time, and no guild matches them) and they are not free: every one of
        # them travels in every backup, and the backup is uploaded after every
        # change. Removing them is the cheapest bytes in the whole file.
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'fixture_rows_v1'"
        ) as cur:
            _fixtures_cleared = await cur.fetchone()
        if not _fixtures_cleared:
            removed = await _prune_fixture_rows(db)
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) "
                "VALUES (0, 'fixture_rows_v1', '1')"
            )
            if removed:
                print(
                    f"[DB] Pruned {removed:,} queued message(s) for a server that "
                    "cannot exist — test fixtures that reached the live database.",
                    flush=True,
                )

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


async def clear_setting(guild_id: int, key: str) -> None:
    """Unset one ``guild_settings`` row by **deleting** it — the table's own word for it.

    The async twin of :func:`write_setting_sync`'s ``None`` branch, and it exists
    because those two are the project's two halves of one table: a setting the
    Flask thread unsets must read as unset to the bot, and vice versa. Writing
    JSON ``null`` instead would *look* identical through :func:`get_setting`
    (``json.loads("null")`` is ``None``) while leaving a row behind, and the
    difference is not cosmetic — an absent row is how every feature's default is
    expressed, so a row that exists and says nothing is a row the next reader has
    to know to ignore.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'DELETE FROM guild_settings WHERE guild_id = ? AND key = ?',
            (guild_id, key),
        )
        await db.commit()


def write_setting_sync(guild_id: int, key: str, value) -> None:
    """One ``guild_settings`` row, written from a thread that cannot await.

    The Flask thread owns the operator's console and cannot drive an event loop,
    so :func:`set_setting` — which is ``async`` — is unreachable from a request.
    The encoding mirrors it exactly (``json.dumps``), because these rows predate
    JSON and :func:`get_setting` / :func:`read_setting_sync` decode with
    ``json.loads``: a writer that stored a bare string would produce a setting
    whose *value* is the string `"1"` on one reader and ``1`` on the other.

    ``None`` deletes the row rather than storing JSON ``null``, which is what
    "unset" means everywhere else in this table (an absent row is how every
    feature's default is expressed).
    """
    if value is None:
        run_atomic(
            lambda cursor: cursor.execute(
                "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
                (int(guild_id), str(key)),
            )
        )
        return
    run_atomic(
        lambda cursor: cursor.execute(
            "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
            (int(guild_id), str(key), json.dumps(value)),
        )
    )

async def open_ticket_feedback(
    guild_id: int,
    channel_id: int,
    author_id: int | None,
    claimed_by: int | None,
    category: str,
) -> None:
    """Reserve the feedback slot for a closing ticket (safe to repeat).

    The rating lands later, in :func:`record_ticket_feedback`. Writing the row
    at close time is what lets a rating answered from a DM still find the people
    to tag once the ticket channel has been deleted.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO ticket_feedback "
            "(ticket_channel_id, guild_id, author_id, claimed_by, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, guild_id, author_id, claimed_by, category),
        )
        # A ticket whose delete failed can be closed twice. Refresh the details
        # so a staff member who claimed it in between is still credited — but
        # never overwrite a rating that has already been given.
        await conn.execute(
            "UPDATE ticket_feedback SET claimed_by = ?, category = ? "
            "WHERE ticket_channel_id = ? AND rating IS NULL",
            (claimed_by, category, channel_id),
        )
        await conn.commit()


async def record_ticket_feedback(channel_id: int, rating: int, comment: str) -> bool:
    """Store a ticket rating. ``False`` when that ticket was already rated.

    The conditional ``UPDATE`` is the whole duplicate guard: two prompts (the
    ticket channel and the opener's DM) can be open at once, and only the first
    one to arrive changes a row.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "UPDATE ticket_feedback SET rating = ?, comment = ? "
            "WHERE ticket_channel_id = ? AND rating IS NULL",
            (rating, comment, channel_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_ticket_feedback(channel_id: int) -> dict | None:
    """The close-time record for a ticket channel, or ``None`` if it never closed."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ticket_feedback WHERE ticket_channel_id = ?", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row is not None else None


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




def read_plan_sync(guild_id: int) -> dict | None:
    """The licence row for a server, or ``None`` when it never activated one.

    Raises on failure so a caller can tell "no licence" from "could not read":
    ``utils.plans`` treats a read error as the free plan, and silently returning
    ``None`` for a broken database would look identical to an honest free tier.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            '''SELECT plan, license_key, instance_id, license_status,
                      activation_limit, activation_usage, customer_email,
                      activated_by, activated_at, checked_at
                 FROM guild_plans WHERE guild_id = ?''',
            (guild_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = (
            "plan", "license_key", "instance_id", "license_status",
            "activation_limit", "activation_usage", "customer_email",
            "activated_by", "activated_at", "checked_at",
        )
        return dict(zip(columns, row))
    finally:
        conn.close()


def write_plan_sync(guild_id: int, row: dict) -> bool:
    """Record a server's activated licence. Returns True on success."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('''INSERT INTO guild_plans
                (guild_id, plan, license_key, instance_id, license_status,
                 activation_limit, activation_usage, customer_email, activated_by,
                 activated_at, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                plan = excluded.plan,
                license_key = excluded.license_key,
                instance_id = excluded.instance_id,
                license_status = excluded.license_status,
                activation_limit = excluded.activation_limit,
                activation_usage = excluded.activation_usage,
                customer_email = excluded.customer_email,
                activated_by = excluded.activated_by,
                activated_at = CURRENT_TIMESTAMP,
                checked_at = CURRENT_TIMESTAMP''',
            (
                int(guild_id), str(row.get("plan") or ""), str(row.get("license_key") or ""),
                str(row.get("instance_id") or ""), str(row.get("license_status") or ""),
                int(row.get("activation_limit") or 0), int(row.get("activation_usage") or 0),
                str(row.get("customer_email") or ""),
                row.get("activated_by"),
            ),
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[PLANS] ❌ Failed to save the plan for guild {guild_id}: {e}", flush=True)
        return False
    finally:
        conn.close()


def write_plan_checked_sync(guild_id: int, *, status: str, activation_limit: int,
                            activation_usage: int) -> bool:
    """Record the result of re-validating a licence.

    Only the fields the licence API answers with are touched — the plan and the
    key stay as they were, so a cancelled subscription shows as non-active
    without losing which licence it was.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('''UPDATE guild_plans
                         SET license_status = ?, activation_limit = ?,
                             activation_usage = ?, checked_at = CURRENT_TIMESTAMP
                       WHERE guild_id = ?''',
                    (str(status or ""), int(activation_limit or 0),
                     int(activation_usage or 0), int(guild_id)))
        conn.commit()
        return True
    except Exception as e:
        print(f"[PLANS] ❌ Failed to record the licence check for guild {guild_id}: {e}", flush=True)
        return False
    finally:
        conn.close()


def clear_plan_sync(guild_id: int) -> bool:
    """Forget a server's licence (after the store released the activation)."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM guild_plans WHERE guild_id = ?', (int(guild_id),))
        conn.commit()
        return True
    except Exception as e:
        print(f"[PLANS] ❌ Failed to clear the plan for guild {guild_id}: {e}", flush=True)
        return False
    finally:
        conn.close()


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

#: The two balances a member holds. A whitelist rather than a free string,
#: because these names reach an f-string in an UPDATE below.
ECONOMY_BALANCE_FIELDS = ("wallet", "bank")

#: The largest single movement the operator's console will make. A bound rather
#: than a policy: nothing in this project limits an owner, and this only stops a
#: mistyped paste from minting a number the bot can no longer add to.
MAX_ECONOMY_ADJUSTMENT = 1_000_000_000_000


def read_operator_modules_sync() -> dict:
    """Every fleet-wide module override, keyed by module id.

    No guild argument, and that is the whole point of the table: the operator's
    answer is about the *product*, not about a server. An empty mapping means
    every system is on its own server's rule.

    Raises on failure, like the guild version, so a reader can tell "nobody has
    overridden anything" from "the row could not be read" — the second one
    answered as the first would quietly release a lock the operator set.
    """
    overrides = {}
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT module_name, enabled FROM operator_modules")
        for row in cur.fetchall():
            overrides[row[0]] = bool(row[1])
    finally:
        conn.close()
    return overrides


def write_operator_module_sync(module_id: str, enabled) -> bool:
    """Set or clear one fleet-wide override. ``None`` clears it.

    Clearing *deletes* the row rather than storing a third value: "auto" is the
    absence of an opinion, and a row holding one would be a second way to say
    the same thing that a reader has to know about.

    Returns whether the write landed, because the console that calls this draws
    the state it just wrote — and a page that reports a lock it did not set is
    how an operator comes to believe a system is closed when it is not.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        if enabled is None:
            cur.execute("DELETE FROM operator_modules WHERE module_name = ?", (str(module_id),))
        else:
            cur.execute(
                "INSERT OR REPLACE INTO operator_modules (module_name, enabled) VALUES (?, ?)",
                (str(module_id), 1 if enabled else 0),
            )
        conn.commit()
        return True
    except Exception as exc:  # pragma: no cover - depends on filesystem/locks
        print(f"[DB] ❌ Could not write the operator override for {module_id}: {exc}", flush=True)
        return False
    finally:
        conn.close()


def read_reaction_roles_sync(guild_id: int) -> list[dict]:
    """Every mapping this server has, for a page that cannot await.

    Raises on failure like the other sync readers, so the dashboard can say
    "could not read" rather than drawing an empty list that looks like a server
    with no reaction roles.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT message_id, emoji, role_id, channel_id FROM reaction_roles "
            "WHERE guild_id = ? ORDER BY message_id, emoji",
            (int(guild_id),),
        )
        return [
            {
                "message_id": int(row[0]),
                "emoji": str(row[1]),
                "role_id": int(row[2]),
                "channel_id": int(row[3] or 0),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def write_reaction_role_sync(
    guild_id: int,
    message_id: int,
    channel_id: int,
    emoji: str,
    role_id: int,
) -> None:
    """One mapping, written from a thread that cannot await (the dashboard)."""
    run_atomic(
        lambda cursor: cursor.execute(
            "INSERT OR REPLACE INTO reaction_roles "
            "(message_id, emoji, role_id, guild_id, channel_id) VALUES (?, ?, ?, ?, ?)",
            (int(message_id), str(emoji), int(role_id), int(guild_id), int(channel_id or 0)),
        )
    )


def delete_reaction_role_sync(message_id: int, emoji: str) -> int:
    """Forget one mapping from the dashboard; answers how many rows went."""

    def work(cursor) -> int:
        cursor.execute(
            "DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (int(message_id), str(emoji)),
        )
        return int(cursor.rowcount or 0)

    return int(run_atomic(work) or 0)


def read_setting_sync(guild_id: int, key: str, default=None):
    """One ``guild_settings`` value, without an event loop.

    The Flask thread cannot await, and a setting read from a *page* has to be
    the same value the bot's own command would see — so this mirrors
    ``get_setting``'s decoding (JSON where it parses, the raw row where it does
    not, because these rows predate JSON) rather than inventing a second rule
    about what a stored setting means.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (int(guild_id), key),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


def read_guild_economy_sync(guild_id: int, limit: int = 25) -> dict:
    """One server's economy: what is in circulation, and who holds it.

    Read-only, and deliberately so. ``cogs/economy.py`` owns every *write* to
    these balances, and this is the one place the operator's console reads them
    — a SELECT, so the two cannot disagree about a number that was never written
    here.

    Three figures rather than one, because they are three different claims:
    everything a member's wallet holds, everything in their bank, and the coins
    **DaCat Bank** has collected in fees. The last of those is the interesting
    one, and it is the one a page can get wrong by forgetting it exists — a fee
    is banked rather than burned (see ``utils/meowfee.py``), so coins that have
    left every member's balance are still in the server, and a total that
    omitted them would be a number that does not add up.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(wallet), 0), COALESCE(SUM(bank), 0), COUNT(*) "
            "FROM economy WHERE guild_id = ?",
            (int(guild_id),),
        )
        wallet, bank, accounts = cur.fetchone()
        cur.execute(
            "SELECT user_id, wallet, bank FROM economy WHERE guild_id = ? "
            "ORDER BY (wallet + bank) DESC, user_id ASC LIMIT ?",
            (int(guild_id), int(limit)),
        )
        top = [
            {
                "user_id": int(user_id),
                "wallet": int(row_wallet or 0),
                "bank": int(row_bank or 0),
                "net": int(row_wallet or 0) + int(row_bank or 0),
            }
            for user_id, row_wallet, row_bank in cur.fetchall()
        ]
        # The bank table is created at boot and defensively on first use, so a
        # database restored from a backup taken before it existed has none. An
        # absent bank is "nothing was ever collected", not a read failure.
        collected = 0
        try:
            cur.execute("SELECT coins FROM meow_bank WHERE guild_id = ?", (int(guild_id),))
            row = cur.fetchone()
            collected = int(row[0]) if row else 0
        except sqlite3.Error:
            collected = 0
    finally:
        conn.close()
    return {
        "wallet": int(wallet or 0),
        "bank": int(bank or 0),
        "circulation": int(wallet or 0) + int(bank or 0),
        "accounts": int(accounts or 0),
        "meow_bank": collected,
        "top": top,
    }


def read_guild_levels_sync(guild_id: int, limit: int = 25) -> dict:
    """One server's progression: how it is configured, who is on top, how far along.

    Read-only, and the companion of :func:`read_guild_economy_sync` for the second
    half of what a server's members earn. Three tables are involved and each
    answers a different question: ``level_settings`` is the *rule* (what a message
    and a voice minute are worth, and where level-ups are announced),
    ``level_roles`` is the *promise* (which role a level grants), and
    ``user_levels`` is the *result*.

    The rules are the ones ``cogs/leveling.py`` reads and writes, with the same
    defaults, because a page that printed a different default than the bot would
    apply would be a page that disagrees with the bot about the server it is
    describing. The one difference is that this does **not** insert the missing row:
    a dashboard that a member of staff opens should not be the thing that creates
    state, and the bot creates it the first time it pays anything out.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        settings = {
            "enabled": True,
            "channel_id": 0,
            "dm_notifications": False,
            "text_min_coins": 4,
            "text_max_coins": 10,
            "text_xp": 15,
            "vc_coins_per_min": 2,
            "vc_xp_per_min": 3,
            "stored": False,
        }
        try:
            cur.execute(
                "SELECT enabled, channel_id, dm_notifications, text_min_coins, "
                "text_max_coins, text_xp, vc_coins_per_min, vc_xp_per_min "
                "FROM level_settings WHERE guild_id = ?",
                (int(guild_id),),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            row = None
        if row:
            settings.update(
                enabled=bool(row[0]),
                channel_id=int(row[1] or 0),
                dm_notifications=bool(row[2]),
                text_min_coins=int(row[3] if row[3] is not None else 4),
                text_max_coins=int(row[4] if row[4] is not None else 10),
                text_xp=int(row[5] if row[5] is not None else 15),
                vc_coins_per_min=int(row[6] if row[6] is not None else 2),
                vc_xp_per_min=int(row[7] if row[7] is not None else 3),
                stored=True,
            )

        # The order is the level's, because a level's role is the reading — a map
        # keyed by role would have to be sorted by something it does not carry.
        roles: list[dict] = []
        try:
            cur.execute(
                "SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level ASC",
                (int(guild_id),),
            )
            roles = [
                {"level": int(level or 0), "role_id": int(role_id or 0)}
                for level, role_id in cur.fetchall()
            ]
        except sqlite3.Error:
            roles = []

        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(xp), 0), COALESCE(SUM(level), 0), "
            "COALESCE(MAX(level), 0), COALESCE(SUM(total_messages), 0), "
            "COALESCE(SUM(voice_minutes), 0) FROM user_levels WHERE guild_id = ?",
            (int(guild_id),),
        )
        members, xp_total, level_sum, highest, messages, voice_minutes = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM user_levels WHERE guild_id = ? AND xp > 0",
            (int(guild_id),),
        )
        earned = cur.fetchone()[0]
        cur.execute(
            "SELECT user_id, xp, level, total_messages, voice_minutes, total_earned "
            "FROM user_levels WHERE guild_id = ? "
            "ORDER BY xp DESC, user_id ASC LIMIT ?",
            (int(guild_id), int(limit)),
        )
        top = [
            {
                "user_id": int(user_id),
                "xp": int(row_xp or 0),
                # The stored level and the XP are two readings of the same thing,
                # and a row written by an older build can disagree with its own
                # XP — so the band is computed from the XP, which is the figure
                # every other surface derives from (see ``utils/levels.py``).
                "level": levels.calculate_level_from_xp(int(row_xp or 0))[0],
                "stored_level": int(row_level or 0),
                "messages": int(row_messages or 0),
                "voice_minutes": int(row_voice or 0),
                "earned": int(row_earned or 0),
            }
            for user_id, row_xp, row_level, row_messages, row_voice, row_earned in cur.fetchall()
        ]
    finally:
        conn.close()

    members = int(members or 0)
    return {
        "settings": settings,
        "roles": roles,
        "top": top,
        "members": members,
        # "Members" counts rows, and a row exists for anybody who has ever been
        # seen — so the number that means something is the second one.
        "earned": int(earned or 0),
        "xp_total": int(xp_total or 0),
        "highest": int(highest or 0),
        "average_level": (int(level_sum or 0) / members) if members else 0.0,
        "messages": int(messages or 0),
        "voice_minutes": int(voice_minutes or 0),
    }


def set_level_settings_sync(guild_id: int, **changes) -> dict:
    """Write this server's leveling rules — the row ``/levelsettings`` writes.

    **The clamps are the cog's**, with the same numbers in the same order,
    because a dashboard that accepted a rate the bot then refused to apply would
    be a page agreeing only with itself. ``text_max_coins`` is raised to meet
    ``text_min_coins`` rather than the write being refused (the bot does the
    same), the two per-message figures cannot fall below
    :data:`levels.MIN_REWARD_RATE`, and the voice rates are bounded by the two
    ceilings in :mod:`utils.levels` that the cog's own reader already enforces.

    Only the fields named in ``changes`` move; everything else keeps what is
    stored, or the deployment's default when the server has never saved a row —
    which is why the current reading is taken first rather than the row being
    rebuilt from the request. The row is **created** here when it does not exist:
    unlike :func:`read_guild_levels_sync`, this is somebody asking for the
    settings to be saved, so creating the row is the thing that was asked for.

    Returns the settings now in force, in the reader's own field names and with
    ``stored=True``, so the page that posted can tell the operator what it
    actually stored rather than what it hoped it had.
    """
    guild_id = int(guild_id)
    current = read_guild_levels_sync(guild_id)["settings"]
    merged = {
        "enabled": bool(current.get("enabled", True)),
        "channel_id": int(current.get("channel_id") or 0),
        "dm_notifications": bool(current.get("dm_notifications", False)),
        "text_min_coins": int(current.get("text_min_coins", 4)),
        "text_max_coins": int(current.get("text_max_coins", 10)),
        "text_xp": int(current.get("text_xp", 15)),
        "vc_coins_per_min": int(current.get("vc_coins_per_min", 2)),
        "vc_xp_per_min": int(current.get("vc_xp_per_min", 3)),
    }

    for field, value in changes.items():
        if field not in merged:
            raise ValueError(f"{field} is not a leveling setting")
        if field in ("enabled", "dm_notifications"):
            merged[field] = bool(value)
            continue
        number = int(value)
        if number < 0:
            raise ValueError(f"{field} cannot be negative")
        merged[field] = number

    merged["text_min_coins"] = max(levels.MIN_REWARD_RATE, merged["text_min_coins"])
    merged["text_max_coins"] = max(merged["text_min_coins"], merged["text_max_coins"])
    merged["text_xp"] = max(levels.MIN_REWARD_RATE, merged["text_xp"])
    merged["vc_coins_per_min"] = max(
        levels.MIN_REWARD_RATE,
        min(levels.MAX_VC_COINS_PER_MIN, merged["vc_coins_per_min"]),
    )
    merged["vc_xp_per_min"] = max(
        levels.MIN_REWARD_RATE,
        min(levels.MAX_VC_XP_PER_MIN, merged["vc_xp_per_min"]),
    )
    merged["channel_id"] = max(0, merged["channel_id"])

    row = (
        guild_id,
        int(merged["enabled"]),
        merged["channel_id"],
        int(merged["dm_notifications"]),
        merged["text_min_coins"],
        merged["text_max_coins"],
        merged["text_xp"],
        merged["vc_coins_per_min"],
        merged["vc_xp_per_min"],
    )

    def work(cur):
        cur.execute(
            "INSERT INTO level_settings "
            "(guild_id, enabled, channel_id, dm_notifications, text_min_coins, "
            " text_max_coins, text_xp, vc_coins_per_min, vc_xp_per_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "enabled = excluded.enabled, channel_id = excluded.channel_id, "
            "dm_notifications = excluded.dm_notifications, "
            "text_min_coins = excluded.text_min_coins, "
            "text_max_coins = excluded.text_max_coins, "
            "text_xp = excluded.text_xp, "
            "vc_coins_per_min = excluded.vc_coins_per_min, "
            "vc_xp_per_min = excluded.vc_xp_per_min",
            row,
        )
        return True

    run_atomic(work)
    merged["stored"] = True
    return merged


def move_economy_sync(guild_id: int, user_id: int, field: str, delta: int) -> dict:
    """Credit or debit one balance, and never *claim* a debit that did not happen.

    A debit is **conditional** — ``wallet = wallet - ? WHERE wallet >= ?`` — and
    the updated rowcount decides the answer. That is the rule every other money
    path in this project already follows, and `cogs/economy.py`'s `settle_bet`
    and `/eco-admin remove` both exist because the older
    ``MAX(0, wallet + ?)`` reported a removal that never happened: it clamped at
    zero and confirmed anyway, so "removed 10,000" could describe a wallet that
    held 50. The console must not be the one surface that lies about a balance.

    Returns ``{"ok": bool, "wallet": int, "bank": int}`` — the balances as they
    are *after* the attempt, so a refusal comes back with the number that made
    it a refusal rather than with an error the caller has to interpret.
    """
    if field not in ECONOMY_BALANCE_FIELDS:
        raise ValueError(f"Unknown economy field {field!r}")
    delta = int(delta)
    if delta == 0:
        raise ValueError("An adjustment of zero is not an adjustment")
    if abs(delta) > MAX_ECONOMY_ADJUSTMENT:
        raise ValueError("That amount is larger than any balance this bot can hold")
    guild_id, user_id = int(guild_id), int(user_id)

    def work(cur):
        # The row may not exist: a member who has never run an economy command
        # has no balance at all, and crediting one is how they get their first
        # coins. Inside the transaction, like every other writer here.
        cur.execute(
            "INSERT OR IGNORE INTO economy (user_id, guild_id) VALUES (?, ?)",
            (user_id, guild_id),
        )
        if delta < 0:
            amount = -delta
            cur.execute(
                f"UPDATE economy SET {field} = {field} - ? "
                f"WHERE user_id = ? AND guild_id = ? AND {field} >= ?",
                (amount, user_id, guild_id, amount),
            )
            if cur.rowcount != 1:
                moved = False
            else:
                moved = True
        else:
            cur.execute(
                f"UPDATE economy SET {field} = {field} + ? WHERE user_id = ? AND guild_id = ?",
                (delta, user_id, guild_id),
            )
            moved = True
        row = cur.execute(
            "SELECT wallet, bank FROM economy WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()
        return {
            "ok": moved,
            "wallet": int(row[0] or 0),
            "bank": int(row[1] or 0),
        }

    return run_atomic(work)


def set_economy_sync(guild_id: int, user_id: int, wallet=None, bank=None) -> dict:
    """Set a member's balances to exact figures. ``None`` leaves one alone.

    The operator's own power, in Discord as well as here: `/eco-admin set` is
    owner-only, and this endpoint is reachable only from the operator's console,
    so there is no approval step to skip — the two surfaces refuse the same
    people (see `_require_operator_api`).
    """
    if wallet is None and bank is None:
        raise ValueError("Nothing to set")
    guild_id, user_id = int(guild_id), int(user_id)
    updates = []
    for field, value in (("wallet", wallet), ("bank", bank)):
        if value is None:
            continue
        value = int(value)
        if not 0 <= value <= MAX_ECONOMY_ADJUSTMENT:
            raise ValueError("A balance cannot be negative or that large")
        updates.append((field, value))

    def work(cur):
        cur.execute(
            "INSERT OR IGNORE INTO economy (user_id, guild_id) VALUES (?, ?)",
            (user_id, guild_id),
        )
        cur.execute(
            "UPDATE economy SET "
            + ", ".join(f"{field} = ?" for field, _ in updates)
            + " WHERE user_id = ? AND guild_id = ?",
            [value for _, value in updates] + [user_id, guild_id],
        )
        row = cur.execute(
            "SELECT wallet, bank FROM economy WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()
        return {"ok": True, "wallet": int(row[0] or 0), "bank": int(row[1] or 0)}

    return run_atomic(work)


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

async def get_radio_stay(guild_id: int) -> tuple[bool, int]:
    """Returns (staying, channel_id) for a guild.

    A *seat* rather than a preference: when it is on, the bot holds the channel
    through silence and reclaims it after a restart. It is also the difference
    between a radio and a player, which is why the default is on for a server
    that has never touched the setting — see ``utils.radio.DEFAULT_STAY``.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT enabled, channel_id FROM radio_stay WHERE guild_id = ?', (guild_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return bool(row[0]), int(row[1] or 0)
    return False, 0

async def set_radio_stay(guild_id: int, channel_id: int, enabled: bool):
    """Save the stay setting for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO radio_stay (guild_id, channel_id, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                enabled = excluded.enabled''',
            (guild_id, channel_id, 1 if enabled else 0))
        await db.commit()

# ── Radio ────────────────────────────────────────────────────────────────────
# What this server last chose, so `/radio` with no argument puts *that* back
# on rather than picking for them. The read has no default to guess: an empty
# string is "nothing remembered", which the caller turns into a random station and
# says so, instead of pretending the choice was anybody's.

async def get_radio_last(guild_id: int) -> str:
    """The station key this guild last played, or ``''`` if it never has."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT station FROM radio_last WHERE guild_id = ?', (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return str(row[0] or "") if row else ""


async def set_radio_last(guild_id: int, station: str):
    """Remember the station this guild just put on."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO radio_last (guild_id, station, as_of)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                station = excluded.station,
                as_of = excluded.as_of''',
            (guild_id, station, time.time()))
        await db.commit()


#: The key this setting lives under, and the only place it is spelled. It sits in
#: the generic ``guild_settings`` table rather than in ``radio_last`` on purpose:
#: that row is *what this server last played*, which is written on every start, and
#: a preference sharing a row with it would be rewritten by the very thing it
#: governs. This one changes when somebody asks it to and never otherwise.
RADIO_ADS_KEY = "radio_ads"

#: The three answers, and they are the radio's own vocabulary rather than the
#: player's old one (``ad-free`` / ``any`` / ``strict``, where the old names were
#: ``prefer`` / ``any`` / ``only``). The rename carried the stored values across;
#: :func:`_carry_radio_rename` is where that mapping is written down.
RADIO_ADS_MODES = ("ad-free", "any", "strict")


async def get_radio_ads(guild_id: int) -> str:
    """How this server wants advertising handled: ``ad-free``, ``any`` or ``strict``.

    Anything unrecognised reads as the radio's default, which is what makes a
    hand-edited database and a value from a future version of this file both mean
    *do what you would have done* rather than *refuse to play anything*.
    """
    value = str(await get_setting(guild_id, RADIO_ADS_KEY, "") or "")
    return value if value in RADIO_ADS_MODES else "ad-free"


async def set_radio_ads(guild_id: int, mode: str):
    """Store the mode, refusing anything that is not one of the three.

    The caller is a slash command with fixed choices, so this cannot be reached
    with a bad value through Discord — it exists for the *other* callers (a test, a
    restore, a future panel) where "store whatever string arrived" would put a
    value in the table that only the reader's fallback understands.
    """
    if mode not in RADIO_ADS_MODES:
        raise ValueError(f"{mode!r} is not one of {RADIO_ADS_MODES}")
    await set_setting(guild_id, RADIO_ADS_KEY, mode)


#: The language this server's radio should be in. A **preference**, unlike the mode
#: above: it decides which station the bot picks when nobody names one, and a
#: member who names a station still gets that station. Nothing about it can refuse
#: anybody, which is why there is no table of allowed values here — the vocabulary
#: is the catalogue's, and the catalogue lives in the cog that plays it.
RADIO_LANGUAGE_KEY = "radio_language"

#: How long a stored language name may be. The longest name in the catalogue is
#: thirteen characters (*Mandarin Chinese*); this is room to spell it differently
#: without being room for a hand-edited row to hold a kilobyte.
RADIO_LANGUAGE_MAX = 40


async def get_radio_language(guild_id: int) -> str:
    """The language this server asked its radio to be in, or ``''`` for any.

    Not validated against the catalogue, deliberately: the picker that reads this
    falls back to the whole table for a name the catalogue has never heard of, so a
    value from a future version of the catalogue, a restore, or a hand-edited row
    is *no preference* rather than a server whose radio has stopped playing.
    """
    value = str(await get_setting(guild_id, RADIO_LANGUAGE_KEY, "") or "").strip()
    return value[:RADIO_LANGUAGE_MAX]


async def set_radio_language(guild_id: int, language: str):
    """Store the language, or clear the preference with an empty string."""
    await set_setting(
        guild_id, RADIO_LANGUAGE_KEY, str(language or "").strip()[:RADIO_LANGUAGE_MAX]
    )

# ── Warnings ─────────────────────────────────────────────────────────────────
# Three helpers where the commands used to hold the SQL directly. The website
# became a second caller, and a second copy of "what a warning is" is how a panel
# and a command end up disagreeing about whether somebody has one.

async def add_reaction_role(
    guild_id: int,
    message_id: int,
    channel_id: int,
    emoji: str,
    role_id: int,
) -> None:
    """Link one emoji on one message to one role — the row the listener reads.

    ``INSERT OR REPLACE`` rather than an insert: re-adding the same pair is how
    somebody *changes* the role an emoji hands out, which is a correction rather
    than a conflict. Options when first registered already reacted for the
    member who asked (see ``cogs/reaction_roles.py``), so a replace changes the
    mapping without touching the reaction on the message.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO reaction_roles "
            "(message_id, emoji, role_id, guild_id, channel_id) VALUES (?, ?, ?, ?, ?)",
            (int(message_id), str(emoji), int(role_id), int(guild_id), int(channel_id or 0)),
        )
        await db.commit()


async def remove_reaction_role(message_id: int, emoji: str) -> int:
    """Forget one mapping; answers how many rows went.

    A count rather than a boolean because the two callers word their answer
    differently: the slash command says "nothing was mapped there" when it is
    zero, and the dashboard says the same in its own sentence.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (int(message_id), str(emoji)),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def reaction_role_for(message_id: int, emoji: str):
    """The mapping a reaction just landed on, as ``(role_id, guild_id)`` or None.

    One row rather than a list: (message, emoji) is the primary key, so the
    listener either has an answer or has nothing to do.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role_id, guild_id FROM reaction_roles "
            "WHERE message_id = ? AND emoji = ?",
            (int(message_id), str(emoji)),
        ) as cursor:
            return await cursor.fetchone()


async def list_reaction_roles(guild_id: int) -> list[dict]:
    """Every mapping this server has, oldest message id first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT message_id, emoji, role_id, channel_id FROM reaction_roles "
            "WHERE guild_id = ? ORDER BY message_id, emoji",
            (int(guild_id),),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "message_id": int(row[0]),
            "emoji": str(row[1]),
            "role_id": int(row[2]),
            "channel_id": int(row[3] or 0),
        }
        for row in rows
    ]


async def add_warning(guild_id: int, user_id: int, reason: str) -> int:
    """Record one warning, and hand back its row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO warnings (guild_id, user_id, reason) VALUES (?, ?, ?)",
            (int(guild_id), int(user_id), str(reason)),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    """One member's warnings, newest first, as rows the panel can draw."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, reason FROM warnings WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC",
            (int(guild_id), int(user_id)),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"id": int(row[0]), "reason": str(row[1] or "")} for row in rows]


async def clear_warnings(guild_id: int, user_id: int) -> int:
    """Delete every warning for one member, reporting how many went."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


# ── What the bot itself wrote on a channel ───────────────────────────────────
#
# The lockdown ledger records what was on a channel *before* a lock, because that is
# the undo. This is the other half: what this bot actually **wrote**, recorded at the
# moment it wrote it, so an overwrite of ours is recognised by its own history long
# after the command that made it has been rewritten or deleted. That history is the
# bug this table exists for — `/unlock` opened rooms by *allowing* `@everyone` to
# talk, the command was rewritten to remove the overwrite instead, and the rooms it
# had already touched kept the allow with nothing anywhere saying whose it was.

OVERWRITE_SCHEMA: str = '''CREATE TABLE IF NOT EXISTS overwrite_writes (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            allow INTEGER NOT NULL DEFAULT 0,
            deny INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            wrote_at REAL NOT NULL DEFAULT 0,
            removed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id, target_id)
        )'''


async def ensure_overwrite_schema(db) -> None:
    """Create the record on a database that predates it, as the lock ledger does."""
    await db.execute(OVERWRITE_SCHEMA)


async def record_overwrites(guild_id, rows: list[tuple], at: float = 0.0) -> int:
    """Remember what this bot just wrote on a channel.

    ``rows`` is ``(channel_id, target_id, allow, deny, reason, removed)`` per
    overwrite, and the latest write for a (channel, target) wins: the record answers
    *what of ours is sitting there now*, not the history of the room. A **removal**
    is a row of its own (``removed=1``) rather than a delete, so an overwrite this
    bot took back is still distinguishable from a channel it never wrote to — the
    two would otherwise read the same, and it is the second the readers speak about.
    """
    if not rows:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_overwrite_schema(db)
        await db.executemany(
            'INSERT OR REPLACE INTO overwrite_writes '
            '(guild_id, channel_id, target_id, allow, deny, reason, wrote_at, removed) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (
                    int(guild_id), int(channel_id), int(target_id), int(allow),
                    int(deny), str(reason or ""), float(at or 0.0),
                    1 if removed else 0,
                )
                for channel_id, target_id, allow, deny, reason, removed in rows
            ],
        )
        await db.commit()
    return len(rows)


async def overwrite_rows(guild_id) -> list[tuple]:
    """``(channel_id, target_id, allow, deny, reason, wrote_at)`` we wrote and kept.

    Rows this bot already took back stay in the table and are left out here: the
    question being asked is *what of this bot's is still on a channel*, and a write
    that has been removed is not an answer to it.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_overwrite_schema(db)
        async with db.execute(
            'SELECT channel_id, target_id, allow, deny, reason, wrote_at '
            'FROM overwrite_writes WHERE guild_id = ? AND removed = 0',
            (int(guild_id),),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        (
            int(row[0]), int(row[1]), int(row[2] or 0), int(row[3] or 0),
            str(row[4] or ""), float(row[5] or 0),
        )
        for row in rows
    ]


# ── A lockdown's ledger ──────────────────────────────────────────────────────

async def save_lockdown(
    guild_id, rows: list[tuple[int, str]], by: int, at: float, *, scope: str = "channel"
) -> int:
    """Write the channels one lockdown touched, with what was there before.

    ``rows`` is ``(channel_id, previous)``. Written before the overwrites are
    applied, because the ledger is the undo: a lockdown that applied and then
    failed to record would be a bulk permission change with no way back.
    ``scope`` is who the lock holds — ``channel`` for the per-channel command,
    ``server`` for the server-wide one — and it is part of the same row because
    the enforcement has to read both answers at once and must never have to guess
    which command wrote a row.
    """
    if not rows:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_lockdown_schema(db)
        await db.executemany(
            'INSERT OR REPLACE INTO lockdown_state '
            '(guild_id, channel_id, previous, locked_at, locked_by, scope) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            [
                (int(guild_id), int(channel_id), str(previous), float(at), int(by), str(scope or "channel"))
                for channel_id, previous in rows
            ],
        )
        await db.commit()
    return len(rows)


#: The scope a `/hide` row carries: the one scope in this table that is **not** a
#: lockdown. The two share ``lockdown_state`` because they share its rule — write
#: down what was here before the change — and the difference is stated here rather
#: than remembered at each reader. A lock's row means *sweep this room*; a hidden
#: room holds nothing, which is why :func:`lockdown_locks` is the reader that has
#: to know: it is the one the message listener asks.
HIDE_SCOPE: str = "hide"


async def lockdown_locks(guild_id) -> list[tuple[int, str]]:
    """``(channel_id, scope)`` for every channel a lockdown holds.

    The enforcement's reading, kept apart from :func:`lockdown_rows` on purpose:
    the undo asks *what was here before*, the sweep asks *who is this held for*,
    and neither should have to carry the other's fields around. A row from before
    the column existed answers ``channel`` — the strict answer.

    A room ``/hide`` took off everybody's list is deliberately **not** here. A
    hidden room holds no sweep: reading one as a lock would delete every message
    posted in it for as long as nobody could see it, which is the opposite of
    leaving somebody's private room alone.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_lockdown_schema(db)
        async with db.execute(
            "SELECT channel_id, scope FROM lockdown_state WHERE guild_id = ? "
            "AND COALESCE(scope, '') != ?",
            (int(guild_id), HIDE_SCOPE),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(row[0]), str(row[1] or "channel")) for row in rows]


async def lockdown_rows(guild_id, *, hides: bool = True) -> list[tuple[int, str, float]]:
    """``(channel_id, previous, locked_at)`` for every channel a lockdown holds.

    ``hides=False`` asks for the **locks** alone, and the undo door is why: 
    `/unlock` puts back what a lockdown changed, while a room somebody hid on
    purpose was hidden for a reason — lifting that by accident would put a room
    back on everybody's list that a person had deliberately taken off it.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_lockdown_schema(db)
        condition = "" if hides else " AND COALESCE(scope, '') != ?"
        params: tuple = (int(guild_id),) if hides else (int(guild_id), HIDE_SCOPE)
        async with db.execute(
            "SELECT channel_id, previous, locked_at FROM lockdown_state "
            f"WHERE guild_id = ?{condition}",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(row[0]), str(row[1] or ""), float(row[2] or 0)) for row in rows]


async def hide_rows(guild_id) -> list[tuple[int, str, float]]:
    """``(channel_id, previous, hidden_at)`` for every room ``/hide`` covers.

    The same table read from the other side, and that is the point rather than a
    shortcut: the write-down-what-was-here rule is one rule, and a second table
    would be a second chance to forget it.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_lockdown_schema(db)
        async with db.execute(
            "SELECT channel_id, previous, locked_at FROM lockdown_state "
            "WHERE guild_id = ? AND COALESCE(scope, '') = ?",
            (int(guild_id), HIDE_SCOPE),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(row[0]), str(row[1] or ""), float(row[2] or 0)) for row in rows]


async def forget_lockdown(guild_id, channel_ids=None) -> int:
    """Forget the ledger — all of it, or the channels that were released."""
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_lockdown_schema(db)
        if channel_ids:
            await db.executemany(
                'DELETE FROM lockdown_state WHERE guild_id = ? AND channel_id = ?',
                [(int(guild_id), int(channel_id)) for channel_id in channel_ids],
            )
        else:
            await db.execute('DELETE FROM lockdown_state WHERE guild_id = ?', (int(guild_id),))
        await db.commit()
    return 0


# ── Alone Time: one locked voice channel per row ─────────────────────────────
#
# The column list lives here once and the two readers below build their answers
# from it, because this table now has three readers in two threads — the cog's
# cache, the dashboard panel and the reconcile loop — and a row shape written out
# three times is a key that goes missing from one of them.
_ALONE_COLUMNS: tuple[str, ...] = (
    "guild_id", "channel_id", "holder_id", "holder_name", "guests",
    "guest_roles", "note", "previous", "previous_limit", "previous_name",
    "locked_as", "keep", "turned_away", "started_at",
)

#: Which of those come back as integers rather than as text.
_ALONE_INT_FIELDS = frozenset({
    "guild_id", "channel_id", "holder_id", "previous_limit", "keep",
    "turned_away",
})


def _alone_row(row) -> dict:
    """One stored row as a plain dict, with the two id lists already split."""
    record = {}
    for index, name in enumerate(_ALONE_COLUMNS):
        value = row[index]
        if name in _ALONE_INT_FIELDS:
            record[name] = int(value or 0)
        elif name in ("guests", "guest_roles"):
            record[name] = tuple(_id_list(value))
        elif name == "started_at":
            record[name] = float(value or 0)
        else:
            record[name] = str(value or "")
    return record


def _id_list(raw) -> list[int]:
    """The ids in one comma-separated column, ignoring anything unreadable."""
    out: list[int] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return out


def _alone_list(values) -> str:
    """One id list as the column stores it — de-duplicated and ordered."""
    seen: list[int] = []
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number and number not in seen:
            seen.append(number)
    return ",".join(str(number) for number in seen)


async def save_alone_time(
    guild_id,
    channel_id,
    *,
    holder_id: int = 0,
    holder_name: str = "",
    guests=(),
    guest_roles=(),
    previous: str = "",
    previous_limit: int = 0,
    previous_name: str = "",
    locked_as: str = "",
    keep: bool = False,
    started_at: float = 0.0,
) -> None:
    """Record a lock **before** its permissions are written, and remember the room.

    One row per channel, replaced wholesale: a second lock on a room that is
    somehow still locked must not merge two ledgers, or a release would restore
    half of one and half of the other.

    **There is no ``note`` argument, and that is the privacy promise expressed as
    a shape.** The holder's reason is deliberately absent from this table — it is
    session memory in ``utils/alonetime`` and nowhere else — so the strongest way
    to keep it out of the file is to leave the writer without a way to put it
    there. The ``note`` column is written blank on every insert and blanked again
    at boot (see the alone_time schema), which is what stops a lock taken before
    the change from holding somebody's words after the upgrade.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO alone_time ('
            + ", ".join(_ALONE_COLUMNS)
            + ') VALUES (' + ", ".join("?" for _ in _ALONE_COLUMNS) + ')',
            (
                int(guild_id), int(channel_id), int(holder_id or 0), str(holder_name or ""),
                _alone_list(guests), _alone_list(guest_roles), "",
                str(previous or ""), int(previous_limit or 0), str(previous_name or ""),
                str(locked_as or ""),
                1 if keep else 0, 0, float(started_at or 0),
            ),
        )
        await db.commit()


async def alone_time_rows(guild_id) -> list[dict]:
    """Every room this server currently has locked, oldest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT " + ", ".join(_ALONE_COLUMNS)
            + " FROM alone_time WHERE guild_id = ? ORDER BY started_at",
            (int(guild_id),),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_alone_row(row) for row in rows]


async def alone_time_row(guild_id, channel_id) -> dict | None:
    """One locked room's row, or ``None``."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT " + ", ".join(_ALONE_COLUMNS)
            + " FROM alone_time WHERE guild_id = ? AND channel_id = ?",
            (int(guild_id), int(channel_id)),
        ) as cursor:
            row = await cursor.fetchone()
    return _alone_row(row) if row is not None else None


async def all_alone_time_rows() -> list[dict]:
    """Every locked room the bot holds, across every server — the fleet reading."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT " + ", ".join(_ALONE_COLUMNS) + " FROM alone_time ORDER BY started_at"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_alone_row(row) for row in rows]


async def write_alone_guests(guild_id, channel_id, guests=(), guest_roles=()) -> None:
    """Replace one locked room's guest list — and nothing else about it."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alone_time SET guests = ?, guest_roles = ? "
            "WHERE guild_id = ? AND channel_id = ?",
            (_alone_list(guests), _alone_list(guest_roles), int(guild_id), int(channel_id)),
        )
        await db.commit()


async def set_alone_locked_as(guild_id, channel_id, name) -> None:
    """Remember what a locked room was renamed to, so a release can undo it.

    One column's worth of news about a row that is already there, rather than a
    re-insert through :func:`save_alone_time`: writing the whole row again would
    need every other field read back first, and a guest list added in between
    would be lost by the read-modify-write.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alone_time SET locked_as = ? WHERE guild_id = ? AND channel_id = ?",
            (str(name or ""), int(guild_id), int(channel_id)),
        )
        await db.commit()


async def bump_alone_turned_away(guild_id, channel_id) -> int:
    """Count one member turned out of a locked room. Answers the new total."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alone_time SET turned_away = turned_away + 1 "
            "WHERE guild_id = ? AND channel_id = ?",
            (int(guild_id), int(channel_id)),
        )
        await db.commit()
        async with db.execute(
            "SELECT turned_away FROM alone_time WHERE guild_id = ? AND channel_id = ?",
            (int(guild_id), int(channel_id)),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def forget_alone_time(guild_id, channel_ids=None) -> int:
    """Forget locked rooms — all of a server's, or the ones that were released."""
    async with aiosqlite.connect(DB_PATH) as db:
        if channel_ids:
            await db.executemany(
                "DELETE FROM alone_time WHERE guild_id = ? AND channel_id = ?",
                [(int(guild_id), int(channel_id)) for channel_id in channel_ids],
            )
        else:
            await db.execute("DELETE FROM alone_time WHERE guild_id = ?", (int(guild_id),))
        await db.commit()
    return 0


#: The same rows, read from a thread that cannot await (the dashboard panel and
#: the cog's cache). Synchronous for the reason ``read_setting_sync`` is: both
#: threads must see one rule about what a stored row means, and the panel cannot
#: drive an event loop.
def read_alone_time_rows_sync(guild_id) -> list[dict]:
    """Every locked room in one server, without an event loop."""
    conn = connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT " + ", ".join(_ALONE_COLUMNS)
            + " FROM alone_time WHERE guild_id = ? ORDER BY started_at",
            (int(guild_id),),
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_alone_row(row) for row in rows]


# ── The operator's blocklist ─────────────────────────────────────────────────

async def block_guild(guild_id, reason: str, by: str, at: float) -> None:
    """Refuse this server the bot, until somebody says otherwise."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO guild_blocklist (guild_id, reason, added_at, added_by) '
            'VALUES (?, ?, ?, ?)',
            (int(guild_id), str(reason or ""), float(at), str(by or "")),
        )
        await db.commit()


async def unblock_guild(guild_id) -> int:
    """Take a server off the blocklist, reporting whether it was on it."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('DELETE FROM guild_blocklist WHERE guild_id = ?', (int(guild_id),))
        await db.commit()
        return int(cursor.rowcount or 0)


async def blocked_guild(guild_id) -> dict | None:
    """This server's blocklist row, or ``None``."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT reason, added_at, added_by FROM guild_blocklist WHERE guild_id = ?',
            (int(guild_id),),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {"reason": str(row[0] or ""), "added_at": float(row[1] or 0), "added_by": str(row[2] or "")}



# ── What the website did ─────────────────────────────────────────────────────

async def record_web_action(
    *, scope: str, actor_id, actor_name: str, guild_id, action: str,
    target: str = "", ok: bool = False, detail: str = "", at: float = 0.0,
) -> None:
    """Write one line of the website's own audit trail. Never raises: a record
    that can fail the action it describes is worse than a missing record."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                'INSERT INTO web_actions '
                '(at, scope, actor_id, actor_name, guild_id, action, target, ok, detail) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    float(at or 0), str(scope or ""), int(actor_id or 0), str(actor_name or "")[:80],
                    int(guild_id or 0), str(action or "")[:60], str(target or "")[:120],
                    1 if ok else 0, str(detail or "")[:400],
                ),
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[AUDIT] could not record '{action}': {exc}", flush=True)


async def recent_web_actions(*, guild_id=None, limit: int = 50) -> list[dict]:
    """The newest website actions, for one server or for the whole fleet."""
    query = (
        'SELECT at, scope, actor_id, actor_name, guild_id, action, target, ok, detail '
        'FROM web_actions'
    )
    args: tuple = ()
    if guild_id not in (None, "", 0, "0"):
        query += ' WHERE guild_id = ?'
        args = (int(guild_id),)
    query += ' ORDER BY at DESC, id DESC LIMIT ?'
    args = args + (int(limit),)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, args) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "at": float(row[0] or 0),
            "scope": str(row[1] or ""),
            "actor_id": str(row[2] or ""),
            "actor_name": str(row[3] or ""),
            "guild_id": str(row[4] or ""),
            "action": str(row[5] or ""),
            "target": str(row[6] or ""),
            "ok": bool(row[7]),
            "detail": str(row[8] or ""),
        }
        for row in rows
    ]


async def radio_seats() -> list[tuple[int, int]]:
    """``(guild_id, channel_id)`` for every server whose radio is a 24/7 seat."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT guild_id, channel_id FROM radio_stay WHERE enabled = 1') as cur:
            rows = await cur.fetchall()
            return [(int(r[0]), int(r[1] or 0)) for r in rows]


#: The shortest a real Discord guild id can be. Snowflakes have been seventeen
#: digits since 2016 and gain a digit every few years; a shorter number is a
#: fixture, a placeholder, or somebody's test.
SNOWFLAKE_MIN_DIGITS = 17


async def _prune_fixture_rows(db) -> int:
    """Delete queued messages whose server cannot exist. Returns how many went.

    Kept as its own function rather than inlined into ``setup`` so the rule can
    be read, and tested, on its own — see :data:`SNOWFLAKE_MIN_DIGITS` for why
    the test is the shape of an id rather than one particular fixture number.
    """
    doomed = (
        "guild_id IS NULL OR LENGTH(CAST(guild_id AS TEXT)) < :min_digits"
    )
    async with db.execute(
        f"SELECT COUNT(*) FROM scheduled_messages WHERE {doomed}",
        {"min_digits": SNOWFLAKE_MIN_DIGITS},
    ) as cur:
        row = await cur.fetchone()
    count = int(row[0] or 0) if row else 0
    if count:
        await db.execute(
            f"DELETE FROM scheduled_messages WHERE {doomed}",
            {"min_digits": SNOWFLAKE_MIN_DIGITS},
        )
        # It deletes, so it commits. The first version of this relied on the
        # caller's transaction, which meant a caller that never committed got a
        # count of rows it had not actually removed — and the test that caught it
        # was reading the table rather than the return value, which is the only
        # way that shows up.
        await db.commit()
    return count


async def _carry_radio_rename(db) -> None:
    """Carry a pre-rename database's radio settings to their new names, once.

    The rule this project already follows for a rescued database — see the market
    rescale below the tables — is that a rename has to be carried rather than
    asked for, and that the *old* rows are left exactly where they are. Two
    reasons, and both matter: a rollback to the release before the rename finds
    its own data untouched, and a second boot copies nothing because the new rows
    already exist (every statement here is ``INSERT OR IGNORE``).

    The advertising vocabulary changed with the setting's name — ``prefer``
    became ``ad-free`` and ``only`` became ``strict`` — so that column is mapped
    rather than copied. The mapping is idempotent: the new words are not in the
    mapping, so a second pass leaves them alone.
    """
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('music_247', 'music_radio')"
    ) as cur:
        old_tables = {str(row[0]) for row in await cur.fetchall()}
    if "music_247" in old_tables:
        await db.execute(
            'INSERT OR IGNORE INTO radio_stay (guild_id, channel_id, enabled) '
            'SELECT guild_id, channel_id, enabled FROM music_247'
        )
    if "music_radio" in old_tables:
        await db.execute(
            'INSERT OR IGNORE INTO radio_last (guild_id, station, as_of) '
            'SELECT guild_id, station, as_of FROM music_radio'
        )
    await db.execute(
        "INSERT OR IGNORE INTO guild_settings (guild_id, key, value) "
        "SELECT guild_id, 'radio_language', value FROM guild_settings "
        "WHERE key = 'music_radio_language'"
    )
    await db.execute(
        "INSERT OR IGNORE INTO guild_settings (guild_id, key, value) "
        "SELECT guild_id, 'radio_ads', value FROM guild_settings "
        "WHERE key = 'music_radio_ads'"
    )
    async with db.execute(
        "SELECT guild_id, value FROM guild_settings WHERE key = 'radio_ads'"
    ) as cur:
        stored = await cur.fetchall()
    for guild_id, raw in stored:
        try:
            value = json.loads(raw)
        except Exception:  # noqa: BLE001 — a hand-edited row is not a crash
            value = ""
        mapped = {"prefer": "ad-free", "only": "strict"}.get(str(value))
        if mapped:
            await db.execute(
                "UPDATE guild_settings SET value = ? WHERE guild_id = ? AND key = 'radio_ads'",
                (json.dumps(mapped), guild_id),
            )

