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
import sqlite3
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


def _storage_list() -> list[dict]:
    """List all files in the bucket."""
    try:
        req = urllib.request.Request(
            _storage_url(),
            data=_json.dumps({"prefix": "", "limit": 100}).encode(),
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _json.loads(resp.read())
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ Supabase list failed: {exc}", flush=True)
        return []


def _storage_download(name: str) -> bytes | None:
    """Download a file from the bucket, or None on failure."""
    try:
        req = urllib.request.Request(
            _storage_url(name),
            headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
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
    """Delete a file from the bucket."""
    try:
        req = urllib.request.Request(
            _storage_url(name),
            headers=_headers(),
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception:
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

# Module-level state shared with the upload path.
_last_sync_hash: str | None = None
_restore_blocked_reason: str | None = None
_remote_checked: bool = False
_last_snapshot_ts: float = 0.0

_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(msg, flush=True)


def restore_from_cloud() -> bool:
    """Download the newest usable backup from Supabase and write it to disk."""
    global _last_sync_hash, _restore_blocked_reason, _remote_checked

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[CLOUD SYNC] ⚠️ SUPABASE_URL or SUPABASE_SERVICE_KEY not set — skipping restore.", flush=True)
        return False

    _restore_blocked_reason = None
    _remote_checked = True

    objects = _storage_list()
    if not objects:
        print("[CLOUD SYNC] ⚠️ No files in Supabase bucket — starting fresh.", flush=True)
        return False

    # Try current file first, then newest snapshot
    candidates = []
    if any(o["name"] == CURRENT_FILE for o in objects):
        candidates.append(CURRENT_FILE)
    candidates.extend(_snapshot_names(objects))

    for name in candidates:
        data = _storage_download(name)
        if data is None:
            continue
        if not _looks_like_sqlite(data):
            print(f"[CLOUD SYNC] ⚠️ '{name}' is not a valid SQLite database — skipping.", flush=True)
            continue

        temp = f"{db_path()}.tmp"
        with open(temp, "wb") as f:
            f.write(data)

        # Remove stale WAL/SHM before swapping
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(db_path() + suffix)
            except FileNotFoundError:
                pass

        # Safety check: don't restore if the downloaded database is suspiciously small
        # This prevents restoring an empty or corrupted backup over good data
        if len(data) < 1024:  # Less than 1KB is definitely empty/corrupted
            print(f"[CLOUD SYNC] ⚠️ '{name}' is too small ({len(data)} bytes) — likely empty. Skipping.", flush=True)
            os.remove(temp)
            continue

        os.replace(temp, db_path())
        _last_sync_hash = hashlib.sha256(data).hexdigest()
        print(
            f"[CLOUD SYNC] ✅ Restored from Supabase '{name}' ({len(data):,} bytes).",
            flush=True,
        )
        return True

    print("[CLOUD SYNC] ❌ No usable snapshot found in Supabase.", flush=True)
    return False


def restore_blocked_reason() -> str | None:
    return _restore_blocked_reason


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

_upload_lock_active = False


def backup_to_cloud_sync() -> bool:
    """Encrypt and upload the current database to Supabase Storage."""
    global _last_sync_hash, _last_snapshot_ts, _remote_checked, _upload_lock_active

    if _upload_lock_active:
        return False
    _upload_lock_active = True
    try:
        # Safety check: don't backup if the database is suspiciously empty
        # This prevents overwriting a good cloud backup with fresh empty tables
        path = db_path()
        if os.path.exists(path) and os.path.getsize(path) > 0:
            import sqlite3
            try:
                conn = sqlite3.connect(path, timeout=5.0)
                # Check if we have any real data (economy, levels, etc.)
                row_count = 0
                for table in ('economy', 'user_levels', 'warnings', 'mod_cases'):
                    try:
                        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                        row_count += cur.fetchone()[0]
                    except Exception:
                        pass
                conn.close()
                if row_count == 0 and _last_sync_hash is not None:
                    # We have no data BUT we successfully restored something from the remote
                    # This means the restore gave us an empty DB — don't overwrite the cloud copy
                    _warn_once("[CLOUD SYNC] ⚠️ Local database has no user data after restore — skipping backup to protect cloud copy.")
                    return False
            except Exception:
                pass
        return _backup_inner()
    finally:
        _upload_lock_active = False


def _backup_inner() -> bool:
    global _last_sync_hash, _last_snapshot_ts, _remote_checked
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    if _restore_blocked_reason:
        _warn_once(f"[CLOUD SYNC] 🛑 Upload blocked: {_restore_blocked_reason}")
        return False

    path = db_path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False

    # Checkpoint WAL so the main file contains every committed change.
    # Without this, ``open(path, 'rb')`` reads a stale snapshot because
    # SQLite in WAL mode writes to bot.db-wal, not the main file — and the
    # SHA-256 hash would never change, silently skipping every upload.
    try:
        _conn = sqlite3.connect(path, timeout=5.0)
        try:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            _conn.close()
    except Exception:
        pass

    with open(path, "rb") as f:
        raw = f.read()

    current_hash = hashlib.sha256(raw).hexdigest()
    if current_hash == _last_sync_hash:
        return True  # nothing changed

    # Safety: refuse to upload an empty database when the remote has data
    if _remote_checked and _db_looks_empty():
        _warn_once(
            "[CLOUD SYNC] 🛑 Refusing to upload: local database has no economy/level rows "
            "but Supabase already holds a backup. Uploading would overwrite real progress."
        )
        return False

    # Upload current backup
    if not _storage_upload(CURRENT_FILE, raw):
        return False

    # Rotate snapshots
    now = time.time()
    do_snapshot = (now - _last_snapshot_ts) >= SNAPSHOT_INTERVAL

    if do_snapshot:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        snap_name = f"{SNAPSHOT_PREFIX}{stamp}{SNAPSHOT_SUFFIX}"
        _storage_upload(snap_name, raw)
        _last_snapshot_ts = now

        # Prune old snapshots
        objects = _storage_list()
        snaps = _snapshot_names(objects)
        for old in snaps[BACKUP_KEEP:]:
            _storage_delete(old)

    _last_sync_hash = current_hash
    print(
        f"[CLOUD SYNC] ☁️ Backup synced to Supabase ({len(raw):,} bytes).",
        flush=True,
    )
    return True


def _db_looks_empty() -> bool:
    """True when no member has any level or economy row."""
    try:
        conn = sqlite3.connect(db_path(), timeout=5.0)
        try:
            for table in ("user_levels", "economy"):
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                except sqlite3.Error:
                    continue
                if row and row[0]:
                    return False
            return True
        finally:
            conn.close()
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Background loop
# --------------------------------------------------------------------------- #

async def cloud_sync_loop():
    """Sync every 25 seconds.

    Transient errors (network timeouts, rate limits) are logged and skipped
    rather than killing the loop forever.  Only a truly unrecoverable error
    (like the env vars disappearing mid-run) should stop syncing.
    """
    import asyncio
    while True:
        try:
            await asyncio.sleep(25)
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
