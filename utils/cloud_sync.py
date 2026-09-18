"""Cloud database sync using Supabase Storage.

The bot runs on Render's free tier, which loses its filesystem on every
restart.  This module keeps a copy of ``bot.db`` in a private Supabase Storage
bucket so that every restart restores the latest state — no coins, levels, or
moderation cases are lost.

Why Supabase instead of a Gist:

* **Private by default** — a Gist is public unless explicitly made secret, and
  an accidentally-public backup exposes every wallet and warning.  Supabase
  Storage requires the service-role key to read or write.
* **No encryption needed** — the bucket is private and the key never reaches
  the client.  The Gist path needed NaCl encryption on top because the
  transport was public.
* **Simpler API** — raw binary upload/download, no base64 round-trip.

Public API (unchanged from the Gist version):

    restore_from_cloud()          — called once on startup
    backup_to_cloud_sync()        — called every 25 s and after admin changes
    start_cloud_sync_loop(bot)    — starts the background task
    db_path()                     — where the local SQLite file lives
    restore_blocked_reason()      — why uploads are paused, or None
"""
from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import json as _json

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUPABASE_URL: str = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_KEY: str = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
BUCKET: str = "bot-backups"

#: The primary backup file — always the latest state.
CURRENT_FILE: str = "bot.db"
#: Snapshots for point-in-time restore, named ``bot-db-YYYYMMDD-HHMMSS.db``.
SNAPSHOT_PREFIX: str = "bot-db-"
SNAPSHOT_SUFFIX: str = ".db"
#: How many snapshots to keep (oldest pruned first).
BACKUP_KEEP: int = 5
#: Seconds between snapshot rotations.
SNAPSHOT_INTERVAL: int = 3600  # 1 hour

def db_path() -> str:
    """Return the current database path. Always reads live from ``utils.db.DB_PATH``
    so that test helpers patching it are respected."""
    from utils import db
    return db.DB_PATH


# --------------------------------------------------------------------------- #
# Supabase Storage helpers
# --------------------------------------------------------------------------- #

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _storage_url(path: str = "") -> str:
    base = f"{SUPABASE_URL}/storage/v1/object"
    if path:
        return f"{base}/{BUCKET}/{path}"
    return f"{base}/list/{BUCKET}"


def _storage_list_raw() -> list[dict] | None:
    """List the bucket, or None when the call itself failed.

    The difference matters: "the bucket is empty" and "Supabase did not answer"
    used to be the same value (``[]``), so a network blip at boot looked exactly
    like a brand-new server — and the bot started empty and then behaved as if
    there was nothing to protect. Nobody can tell those apart downstream, so the
    failure has to be reported as one.
    """
    try:
        req = urllib.request.Request(
            _storage_url(),
            data=_json.dumps({"prefix": "", "limit": 100}).encode(),
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = _json.loads(resp.read())
            return payload if isinstance(payload, list) else None
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Supabase list failed: {exc}", flush=True)
        return None


def _storage_list() -> list[dict]:
    """List all files in the bucket (empty list on failure)."""
    return _storage_list_raw() or []


def _storage_download(name: str, quiet: bool = False) -> bytes | None:
    """Download a file from the bucket, or None on failure.

    ``quiet`` is for objects that are expected to be missing (the sync lease),
    where a 404 is an answer rather than a problem.
    """
    try:
        req = urllib.request.Request(
            _storage_url(name),
            headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if quiet and exc.code in (400, 404):
            return b""
        if not quiet:
            print(f"[CLOUD SYNC] ⚠️ Supabase download '{name}' failed: {exc}", flush=True)
        return None
    except Exception as exc:
        if not quiet:
            print(f"[CLOUD SYNC] ⚠️ Supabase download '{name}' failed: {exc}", flush=True)
        return None


def _storage_upload(name: str, data: bytes) -> bool:
    """Upload (or overwrite) a file in the bucket.

    Uses PUT (not POST) because Supabase Storage's POST endpoint returns
    409 "The resource already exists" when the file is already in the bucket.
    PUT replaces it silently.
    """
    try:
        req = urllib.request.Request(
            _storage_url(name),
            data=data,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/octet-stream",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status in (200, 201)
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Supabase upload '{name}' failed: {exc}", flush=True)
        return False


def _storage_delete(name: str) -> bool:
    """Delete a file from the bucket.

    No ``Content-Type`` here on purpose: Supabase Storage answers a bodiless
    DELETE that *claims* to carry JSON with ``400 Body cannot be empty when
    content-type is set to 'application/json'``. The shared ``_headers()`` sets
    exactly that, so every prune silently did nothing while reporting success by
    returning False nobody looked at — the bucket grew forever and the "keep the
    newest five snapshots" rule was never once enforced.
    """
    try:
        req = urllib.request.Request(
            _storage_url(name),
            headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as exc:
        detail = exc.read(200).decode("utf-8", "replace").strip()
        print(f"[CLOUD SYNC] ⚠️ Could not delete '{name}': HTTP {exc.code} {detail}", flush=True)
        return False
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Could not delete '{name}': {exc}", flush=True)
        return False


# --------------------------------------------------------------------------- #
# Snapshot naming
# --------------------------------------------------------------------------- #

def _snapshot_names(objects: list[dict]) -> list[str]:
    """Return snapshot filenames sorted newest-first."""
    names = [
        o["name"]
        for o in objects
        if o["name"].startswith(SNAPSHOT_PREFIX) and o["name"].endswith(SNAPSHOT_SUFFIX)
    ]
    return sorted(names, reverse=True)


def _looks_like_sqlite(data: bytes) -> bool:
    return len(data) > 100 and data[:16] == b"SQLite format 3\x00"


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #

#: Tables whose rows prove a database holds real progress.
COUNTED_TABLES: tuple[str, ...] = ("economy", "user_levels", "mod_cases", "warnings", "activity_messages", "activity_voice")

#: Below this many rows a database counts as effectively empty.
MIN_MEANINGFUL_ROWS: int = 3

#: Object holding the single-writer lease.
LEASE_FILE: str = "sync-owner.json"
#: A lease older than this belongs to a process that is no longer running.
#: A redeploy starts writing again within this window at worst.
LEASE_TTL: int = 300
#: How often an idle instance refreshes its lease (seconds).
LEASE_TOUCH_INTERVAL: int = 120
#: Consecutive checks a *shrunk* database is held back before its state is accepted.
SHRINK_GRACE_TICKS: int = 4
#: Escape hatch for a deliberate wipe: ``CLOUD_SYNC_ALLOW_SHRINK=1``.
ALLOW_SHRINK: bool = (os.environ.get("CLOUD_SYNC_ALLOW_SHRINK") or "").strip().lower() in {
    "1", "true", "yes", "on",
}

# Module-level state shared with the upload path.
_last_sync_hash: str | None = None
_restore_blocked_reason: str | None = None
_remote_checked: bool = False
_last_snapshot_ts: float = 0.0
#: When the primary backup last landed, so /dbstatus can say whether sync is alive.
_last_upload_ts: float = 0.0

#: The object this process restored from, or None if it never restored one.
_restored_from: str | None = None
#: Row counts of the database as it was restored, so a wipe can be spotted.
_restored_counts: dict[str, int] = {}
#: True once the bucket is known to already hold a backup.
_cloud_has_data: bool = False
#: One host writes; every other host restores and reads only.
_INSTANCE_ID: str = socket.gethostname() or f"pid-{os.getpid()}"
#: When our lease was last refreshed.
_lease_touched: float = 0.0
#: How many consecutive checks a shrunken database has been held back.
_shrink_refusals: int = 0

_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(msg, flush=True)


def _row_counts(path: str | None = None) -> dict[str, int]:
    """How many rows of real progress the database holds per counted table."""
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(path or db_path(), timeout=5.0)
        try:
            for table in COUNTED_TABLES:
                try:
                    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = 0
        finally:
            conn.close()
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Could not count rows: {exc}", flush=True)
    return counts


def _total_rows(counts: dict[str, int] | None = None) -> int:
    return sum((counts if counts is not None else _row_counts()).values())


def _db_readable(path: str | None = None) -> bool:
    """True when the file opens as SQLite at all.

    An *empty* database and an *unopenable* one are different answers: empty is
    a state a server can legitimately be in, unreadable means the file is not a
    database and must never be uploaded over a good backup.
    """
    try:
        conn = sqlite3.connect(path or db_path(), timeout=5.0)
        try:
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def restore_from_cloud(attempts: int = 3) -> bool:
    """Restore the newest usable backup, retrying before giving up.

    One failed download used to end the attempt for good: the bot came up with
    an empty database, and the next upload overwrote the backup with it. The
    restore is now retried, and if every attempt fails the upload path is left
    **blocked** — an instance that could not read the backup must never write
    over it.
    """
    for attempt in range(1, max(1, attempts) + 1):
        status = _restore_once()
        if status == "restored":
            return True
        if status == "empty":
            return False
        if attempt < attempts:
            delay = 2.0 * attempt
            print(
                f"[CLOUD SYNC] Restore attempt {attempt}/{attempts} failed — "
                f"retrying in {delay:.0f}s.",
                flush=True,
            )
            time.sleep(delay)

    _block_uploads(
        "the cloud backup could not be downloaded at startup, so uploading "
        "could overwrite it with a different database"
    )
    return False


def _restore_once() -> str:
    """One restore attempt. Returns ``restored``, ``empty`` or ``failed``."""
    global _last_sync_hash, _remote_checked, _restored_from, _restored_counts
    global _cloud_has_data

    if not SUPABASE_URL or not SUPABASE_KEY:
        print(
            "[CLOUD SYNC] ⚠️ SUPABASE_URL or SUPABASE_SERVICE_KEY not set — "
            "skipping restore.",
            flush=True,
        )
        return "failed"

    _remote_checked = True

    objects = _storage_list_raw()
    if objects is None:
        return "failed"
    if not objects:
        _cloud_has_data = False
        _clear_block()
        print("[CLOUD SYNC] ⚠️ No files in Supabase bucket — starting fresh.", flush=True)
        return "empty"

    # A backup exists, so from here on this instance is only allowed to upload a
    # descendant of it.
    _cloud_has_data = True

    candidates = []
    if any(o.get("name") == CURRENT_FILE for o in objects):
        candidates.append(CURRENT_FILE)
    candidates.extend(_snapshot_names(objects))

    #: An empty backup is a worse answer than a full one, but it is still an
    #: answer: a server whose backups have never held a row must be able to
    #: come back up and save again, rather than being blocked forever.
    empty_fallback: tuple[str, bytes, dict[str, int]] | None = None

    for name in candidates:
        data = _storage_download(name)
        if data is None:
            continue
        if not _looks_like_sqlite(data) or len(data) < 1024:
            print(
                f"[CLOUD SYNC] ⚠️ '{name}' is not a usable SQLite database "
                f"({len(data):,} bytes) — skipping.",
                flush=True,
            )
            continue

        temp = f"{db_path()}.tmp"
        with open(temp, "wb") as f:
            f.write(data)

        # The downloaded file must hold the tables we count before it replaces a
        # working database, or a corrupt copy would be swapped in and lose
        # everything on the next upload.
        counts = _row_counts(temp)
        if _total_rows(counts) == 0:
            print(
                f"[CLOUD SYNC] ⚠️ '{name}' holds no economy/level rows — "
                "trying the next candidate.",
                flush=True,
            )
            _remove(temp)
            if empty_fallback is None:
                # Newest empty candidate, kept in case every backup is empty.
                empty_fallback = (name, data, counts)
            continue

        _install(name, data, counts, temp=temp)
        return "restored"

    if empty_fallback is not None:
        name, data, counts = empty_fallback
        _install(name, data, counts)
        print(
            "[CLOUD SYNC] ⚠️ Every backup is empty — that is the truth for this "
            "server, so saving is allowed again.",
            flush=True,
        )
        return "restored"

    print("[CLOUD SYNC] ❌ No usable snapshot found in Supabase.", flush=True)
    # The bucket has backups, but none of them could be used. Uploading now
    # would replace them with whatever this process happens to hold.
    _block_uploads("no usable backup could be restored, but the bucket holds one")
    return "failed"


def _install(
    name: str,
    data: bytes,
    counts: dict[str, int],
    temp: str | None = None,
) -> None:
    """Swap a downloaded backup into place and record where it came from.

    ``temp`` is the copy the caller already wrote and counted; reusing it keeps
    the counts that were verified and the bytes that are installed the same file.
    """
    global _last_sync_hash, _restored_from, _restored_counts

    temp = temp or f"{db_path()}.tmp"
    if not os.path.exists(temp):
        with open(temp, "wb") as f:
            f.write(data)

    # Remove stale WAL/SHM before the swap, or SQLite replays the old, empty
    # write-ahead log over the restored file.
    for suffix in ("-wal", "-shm"):
        _remove(db_path() + suffix)

    os.replace(temp, db_path())
    _last_sync_hash = hashlib.sha256(data).hexdigest()
    _restored_from = name
    _restored_counts = counts
    _clear_block()
    print(
        f"[CLOUD SYNC] ✅ Restored '{name}' ({len(data):,} bytes) — "
        + ", ".join(f"{t}={n}" for t, n in counts.items()),
        flush=True,
    )


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _block_uploads(reason: str) -> None:
    global _restore_blocked_reason
    _restore_blocked_reason = reason
    print(f"[CLOUD SYNC] 🛑 Uploads blocked: {reason}", flush=True)


def _clear_block() -> None:
    global _restore_blocked_reason
    _restore_blocked_reason = None


def restore_blocked_reason() -> str | None:
    return _restore_blocked_reason


def last_upload_ts() -> float:
    """Unix time of the last primary backup that landed, or 0 if none ever has."""
    return _last_upload_ts


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

_upload_lock = threading.Lock()
_upload_requested = False


def _snapshot_bytes(path: str) -> bytes | None:
    """A transactionally consistent copy of the database, WAL included.

    Reading the file directly is not enough in WAL mode: every row committed
    since the last checkpoint lives in ``bot.db-wal``, so a raw read uploads a
    database that is *missing the newest progress* — and the next restart
    restores that older state, which is exactly what "my coins went back"
    looks like. SQLite's own backup API copies a consistent snapshot instead,
    and it works while the bot is writing.
    """
    fd, temp = tempfile.mkstemp(prefix="meow-snapshot-", suffix=".db")
    os.close(fd)
    try:
        source = sqlite3.connect(path, timeout=15.0)
        try:
            target = sqlite3.connect(temp)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        with open(temp, "rb") as f:
            return f.read()
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Could not snapshot the database: {exc}", flush=True)
        return None
    finally:
        _remove(temp)


# --------------------------------------------------------------------------- #
# Single writer
# --------------------------------------------------------------------------- #

def _live_other_owner() -> tuple[str, float] | None:
    """The name of another copy that is running now, and how long ago it spoke.

    One place decides what the lease means, because two callers need it for two
    halves of the same problem: the backup refuses to write while another copy
    owns the bucket, and the boot says out loud that another copy is answering
    Discord. The *name* is what the second caller needs, so it is not baked into
    a sentence here.

    ``None`` means every flavour of "I cannot tell": no bucket, no lease, an
    unreadable lease, a lease with no name in it, our own lease, or one whose
    timestamp will not parse. A lease older than :data:`LEASE_TTL` is reported as
    a takeover (the other copy is gone) and also answers ``None``.
    """
    if not _cloud_has_data:
        return None
    raw = _storage_download(LEASE_FILE, quiet=True)
    if not raw:  # missing, or we cannot tell — the restore rule still applies
        return None
    try:
        lease = _json.loads(raw)
    except Exception:
        return None
    owner = str(lease.get("owner") or "")
    if not owner or owner == _INSTANCE_ID:
        return None
    try:
        ts = float(lease.get("ts") or 0)
    except (TypeError, ValueError):
        # A lease nobody can read is not a claim on the bucket, and it is not an
        # owner either — treating the unreadable as stale would invite a
        # takeover of a bucket whose real owner is in fact alive.
        return None
    age = time.time() - ts
    if age > LEASE_TTL:
        print(
            f"[CLOUD SYNC] Taking over the sync lease from '{owner}' "
            f"(idle for {age / 60:.0f} min).",
            flush=True,
        )
        return None
    return owner, age


def _lease_problem() -> str | None:
    """Whether some other live instance owns the bucket."""
    live = _live_other_owner()
    if live is None:
        return None
    owner, age = live
    return (
        f"'{owner}' owns the bucket ({age / 60:.1f} min ago) — running two "
        "instances of the bot against one backup is how progress gets reverted"
    )


def _service_of(instance: str) -> str | None:
    """The hosting *service* an instance name belongs to, when it names one.

    Render names a container ``srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4``:
    the service id, the service's own name, the deployment hash, then the pod. Two
    containers of one deployment share the ``srv-<id>`` head and differ after it.
    """
    parts = (instance or "").split("-")
    if len(parts) >= 3 and parts[0] == "srv":
        return "-".join(parts[:2])
    return None


def _previous_container_of_this_service(owner: str) -> bool:
    """Whether ``owner`` is the last container of *this* deployment, not a copy.

    A rolling deploy starts the new container while the old one is still
    renewing the lease for a few seconds, and each container gets its own
    hostname — so the lease names "another instance" that is really the same
    deployment's previous pod. Reporting that as two copies of the bot is a card
    telling the operator to stop the copy they just deployed: a false alarm on
    the one warning that has to be believed, which is how a real duplicate gets
    ignored by the person who most needs to act on it. The lease's own rule is
    untouched (the new container still waits for the old writer to go), because
    that wait is what stops two writers touching one backup.
    """
    return _service_of(owner) is not None and _service_of(owner) == _service_of(_INSTANCE_ID)


def other_live_copy() -> str | None:
    """A *second copy* of this bot that is serving right now, or ``None``.

    The lease exists so two copies cannot fight over the backup; this asks it the
    other half of the same question. Two copies do not only race over the database
    — they race over the **gateway**, where Discord delivers every interaction to
    both, one answers, and the loser's command ends in ``Interaction has already
    been acknowledged``. The boot reports that, so a duplicated bot is a named
    fault instead of commands that randomly say "the application did not respond".

    Everything that is not a second copy answers ``None``: a lease that cannot be
    read, an empty bucket, a lease older than :data:`LEASE_TTL`, and the previous
    container of this very deployment. What it returns is the other copy's own
    name for itself and nothing else, because it is quoted in a card an operator
    reads.
    """
    live = _live_other_owner()
    if live is None:
        return None
    owner = live[0]
    if _previous_container_of_this_service(owner):
        print(
            f"[INSTANCE] {owner} is this deployment's previous container, still "
            "holding the sync lease — not a second copy of the bot.",
            flush=True,
        )
        return None
    return owner


def _lease_touch(force: bool = False) -> None:
    """Keep our claim on the bucket warm, even when nothing changed."""
    global _lease_touched
    now = time.time()
    if not force and (now - _lease_touched) < LEASE_TOUCH_INTERVAL:
        return
    payload = _json.dumps({
        "owner": _INSTANCE_ID,
        "ts": now,
        "pid": os.getpid(),
    }).encode()
    if _storage_upload(LEASE_FILE, payload):
        _lease_touched = now


# --------------------------------------------------------------------------- #
# Upload decision
# --------------------------------------------------------------------------- #

def _consult_bucket() -> str | None:
    """Look at the bucket once and record what it holds.

    The decision must not depend on the caller having remembered to restore
    first: ``keep_alive``'s dashboard can ask for a backup on its own thread, and
    a guard that only exists on the startup path is a guard with a way around it.
    """
    global _remote_checked, _cloud_has_data

    objects = _storage_list_raw()
    if objects is None:
        return (
            "the bucket could not be listed, so uploading could replace a "
            "backup this instance cannot see"
        )
    _remote_checked = True
    _cloud_has_data = bool(objects)
    return None


def upload_refusal_reason(decide: bool = True) -> str | None:
    """Why this process must not write to the bucket, or None when it may.

    Every rule here exists because the alternative loses somebody's coins:

    0. A failed startup restore blocks writes outright — an instance that could
       not read the only copy must never write over it.
    1. The bucket is consulted before the first write, whatever the call order.
    2. Only a descendant of the backup may overwrite it — an instance that
       never restored is a stranger, and its database is not the truth.
    3. A database that will not open is never uploaded over a working one.
    4. A collapse in row counts is held back for a few checks (long enough for
       a restart or a half-finished write to sort itself out) and then accepted,
       because a server that really was reset has to be able to save again.
    5. One host writes; a second live instance is told to keep its hands off.

    ``decide=False`` answers the same question without spending a grace tick,
    for callers that are only reporting (``/dbstatus``).
    """
    global _shrink_refusals

    if not SUPABASE_URL or not SUPABASE_KEY:
        return "Supabase is not configured"
    if _restore_blocked_reason:
        return _restore_blocked_reason

    if not _remote_checked:
        problem = _consult_bucket()
        if problem:
            return problem

    if _cloud_has_data and _restored_from is None:
        return (
            "this instance never restored the cloud backup, so its database is "
            "not a descendant of the one being protected"
        )

    if not _db_readable():
        return "the local database could not be read as SQLite"

    local_total = _total_rows()
    restored_total = _total_rows(_restored_counts)
    if not ALLOW_SHRINK and restored_total >= MIN_MEANINGFUL_ROWS:
        if local_total * 2 < restored_total:
            if decide:
                _shrink_refusals += 1
            if _shrink_refusals < SHRINK_GRACE_TICKS:
                return (
                    f"rows fell from {restored_total} to {local_total} "
                    f"(check {_shrink_refusals}/{SHRINK_GRACE_TICKS})"
                )
            if decide:
                print(
                    f"[CLOUD SYNC] ⚠️ Row count settled at {local_total} after "
                    f"{_shrink_refusals} checks — accepting it as the real state.",
                    flush=True,
                )
    _shrink_refusals = 0

    return _lease_problem()


def backup_to_cloud_sync() -> bool:
    """Upload a consistent snapshot of the database to Supabase Storage.

    Coalescing rather than dropping. The guard used to be a bare boolean read and
    written without a lock, and a caller that arrived while an upload was in
    flight got a ``False`` and nothing else — which is precisely the case the
    dashboard's "back up right now after a change" call exists for: a restart
    inside the 25-second window restoring an older snapshot and quietly undoing
    the administrator's change. Now the request is remembered and one more upload
    runs before the lock is released.
    """
    global _upload_requested

    if not _upload_lock.acquire(blocking=False):
        _upload_requested = True
        return False
    try:
        result = _backup_inner()
        while _upload_requested:
            _upload_requested = False
            result = _backup_inner() or result
        return result
    finally:
        _upload_lock.release()


def _backup_inner() -> bool:
    global _last_sync_hash, _last_snapshot_ts, _last_upload_ts

    reason = upload_refusal_reason()
    if reason:
        _warn_once(f"[CLOUD SYNC] 🛑 Not uploading: {reason}")
        return False

    path = db_path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False

    # The hash of the snapshot is the only "did anything change?" test. A stat
    # (size + mtime) looks cheaper, but SQLite can rewrite a row in place without
    # moving either — so a stat-based shortcut can decide "unchanged" about a
    # database that changed, and skip the upload of it. Wasted I/O is the better
    # failure here.
    raw = _snapshot_bytes(path)
    if raw is None:
        return False

    current_hash = hashlib.sha256(raw).hexdigest()
    if current_hash == _last_sync_hash:
        _lease_touch()  # alive and unchanged: keep the claim warm, upload nothing
        return True

    if not _storage_upload(CURRENT_FILE, raw):
        return False

    _lease_touch(force=True)
    _last_upload_ts = time.time()

    now = _last_upload_ts
    if (now - _last_snapshot_ts) >= SNAPSHOT_INTERVAL:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        # The clock only moves for a snapshot that actually landed. The old code
        # advanced it regardless of the answer, so a refused upload cost a whole
        # interval with nothing in the log to say a snapshot was missing —
        # "keep the newest 5" quietly became a single file.
        if _storage_upload(f"{SNAPSHOT_PREFIX}{stamp}{SNAPSHOT_SUFFIX}", raw):
            _last_snapshot_ts = now
            stale = _snapshot_names(_storage_list())[BACKUP_KEEP:]
            pruned = sum(1 for old in stale if _storage_delete(old))
            if stale:
                print(
                    f"[CLOUD SYNC] 🧹 Pruned {pruned}/{len(stale)} old snapshot(s).",
                    flush=True,
                )
        else:
            print(
                "[CLOUD SYNC] ⚠️ Snapshot upload refused — retrying next tick.",
                flush=True,
            )

    _last_sync_hash = current_hash
    print(
        f"[CLOUD SYNC] ☁️ Backup synced to Supabase ({len(raw):,} bytes).",
        flush=True,
    )
    return True


def _db_looks_empty() -> bool:
    """True when no member has any level or economy row."""
    return _total_rows() == 0


# --------------------------------------------------------------------------- #
# Background loop
# --------------------------------------------------------------------------- #

#: How often an instance that could not restore retries while it is empty.
RESTORE_RETRY_INTERVAL: int = 120


async def cloud_sync_loop():
    """Sync every 25 seconds.

    Transient errors (network timeouts, rate limits) are logged and skipped
    rather than killing the loop forever.  Only a truly unrecoverable error
    (like the env vars disappearing mid-run) should stop syncing.
    """
    import asyncio
    last_restore_try = 0.0
    while True:
        try:
            await asyncio.sleep(25)
            # An instance that booted during a Supabase blip is blocked from
            # uploading. While it still holds nothing, picking the backup up
            # late costs nobody anything, so keep trying — otherwise the only
            # cure is a manual restart.
            if _restore_blocked_reason and _total_rows() == 0:
                if time.time() - last_restore_try >= RESTORE_RETRY_INTERVAL:
                    last_restore_try = time.time()
                    print("[CLOUD SYNC] Retrying the startup restore…", flush=True)
                    await asyncio.to_thread(restore_from_cloud, 1)
            await asyncio.to_thread(backup_to_cloud_sync)
        except Exception as exc:
            _warn_once(f"[CLOUD SYNC] ⚠️ Loop error (will retry): {exc}")
            await asyncio.sleep(60)


def start_cloud_sync_loop(bot):
    """Start the async background loop."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[CLOUD SYNC] ⏸️ Loop not started (SUPABASE_URL or SUPABASE_SERVICE_KEY missing).", flush=True)
        return None
    return bot.loop.create_task(cloud_sync_loop())
