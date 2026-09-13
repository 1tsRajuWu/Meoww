"""
Persistent Cloud Database Sync for Meow Discord Bot.

Preserves the SQLite database (bot.db) across cloud deploys, restarts and sleep
cycles using GitHub Gists, so levels, economy, inventory, tickets and module
toggles survive.

Security notes
--------------
* Credentials come from the environment only. Never hardcode a token here —
  this file is public and a committed token is compromised forever.
* When ``BACKUP_ENCRYPTION_KEY`` is set the database is encrypted with
  PyNaCl SecretBox (XSalsa20-Poly1305) *before* it leaves the machine, so a
  leaked Gist URL exposes nothing. Without a key the old plaintext format is
  still written, but loudly warned about.
* Every payload carries a SHA-256 of the plaintext, verified after decryption,
  so a truncated or tampered snapshot is refused rather than restored.
* Rotated snapshots (``bot-db-<timestamp>.enc``) allow point-in-time restore;
  the newest ``BACKUP_KEEP`` are retained.
"""
import os
import time
import base64
import asyncio
import hashlib
import tempfile
import urllib.request
import json

from utils import db

# SECURITY: credentials must come from the environment only.
GITHUB_TOKEN = (os.environ.get("GITHUB_BACKUP_TOKEN") or "").strip()
GIST_ID = (os.environ.get("GIST_BACKUP_ID") or "").strip()


def db_path() -> str:
    """The database path, resolved at call time from ``utils.db``.

    This used to be a second `DB_PATH = "bot.db"` constant living in this file.
    Two copies of a path means they can drift, and a drifting backup path is a
    data-loss bug: the backup would faithfully upload whatever database
    happened to sit at *its* idea of the path. There is now exactly one owner.
    """
    return db.DB_PATH

# Always-current snapshot, plus rotating timestamped ones.
CURRENT_FILE = "bot.db.enc"
SNAPSHOT_PREFIX = "bot-db-"
LEGACY_PLAINTEXT_FILE = "bot.db.b64"

SNAPSHOT_INTERVAL_SECONDS = int(os.environ.get("BACKUP_SNAPSHOT_INTERVAL", "1800"))
BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "5")))

if not GITHUB_TOKEN or not GIST_ID:
    print(
        "[CLOUD SYNC] ⚠️ GITHUB_BACKUP_TOKEN / GIST_BACKUP_ID not set. "
        "Cloud backup is DISABLED, so bot.db will not survive a redeploy. "
        "Set both environment variables to re-enable it.",
        flush=True,
    )

SQLITE_MAGIC = b"SQLite format 3\x00"

_last_sync_hash = None
_last_snapshot_ts = 0.0
# Whether we have successfully listed the remote files this process. The first
# upload after a deploy is the dangerous one, so the remote is inspected then.
_remote_checked = False


# --------------------------------------------------------------------------- #
# Crypto helpers (pure functions — unit-testable without any network access)
# --------------------------------------------------------------------------- #
def encryption_key() -> bytes | None:
    """Decode BACKUP_ENCRYPTION_KEY (base64 of 32 raw bytes), or None."""
    raw = (os.environ.get("BACKUP_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        print("[CLOUD SYNC] ❌ BACKUP_ENCRYPTION_KEY is not valid base64 — backups stay unencrypted.", flush=True)
        return None
    if len(key) != 32:
        print(
            f"[CLOUD SYNC] ❌ BACKUP_ENCRYPTION_KEY must decode to 32 bytes (got {len(key)}) — "
            "backups stay unencrypted.",
            flush=True,
        )
        return None
    return key


def encrypt_bytes(raw: bytes, key: bytes) -> bytes:
    """Return SecretBox ciphertext of ``sha256(raw) + b'\\n' + raw``."""
    from nacl.secret import SecretBox
    from nacl.utils import random as nacl_random

    digest = hashlib.sha256(raw).hexdigest().encode("ascii")
    return bytes(SecretBox(key).encrypt(digest + b"\n" + raw, nacl_random(SecretBox.NONCE_SIZE)))


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    """Inverse of :func:`encrypt_bytes`; raises if the integrity check fails."""
    from nacl.secret import SecretBox

    plain = SecretBox(key).decrypt(blob)
    digest, separator, raw = plain.partition(b"\n")
    if not separator or hashlib.sha256(raw).hexdigest().encode("ascii") != digest:
        raise ValueError("backup integrity check failed (corrupted or tampered payload)")
    return raw


def generate_key() -> str:
    """Convenience for operators: a fresh base64 key."""
    from nacl.utils import random as nacl_random

    return base64.b64encode(nacl_random(32)).decode("ascii")


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _headers(include_content_type: bool = False) -> dict:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "MeowBot-CloudSync",
    }
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _gist_get() -> dict | None:
    try:
        req = urllib.request.Request(f"https://api.github.com/gists/{GIST_ID}", headers=_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[CLOUD SYNC] ⚠️ Could not read the backup Gist: {e}", flush=True)
        return None


def _gist_patch(payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_headers(include_content_type=True),
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        print(f"[CLOUD SYNC] ⚠️ Error syncing database to cloud: {e}", flush=True)
        return False


def snapshot_db_bytes(path: str | None = None) -> bytes | None:
    """Return a consistent copy of the live database as bytes.

    Reading ``bot.db`` directly is not safe: the database runs in WAL mode, so
    recently committed rows can still be sitting in ``bot.db-wal`` and a plain
    file read would silently upload a database missing the newest progress.
    SQLite's backup API produces a transactionally consistent snapshot that
    includes the write-ahead log, even while the bot is writing.
    """
    import sqlite3

    source_path = path or db_path()
    if not os.path.exists(source_path):
        return None

    handle, temp_path = tempfile.mkstemp(prefix="meow-snapshot-", suffix=".db")
    os.close(handle)
    try:
        source = sqlite3.connect(source_path, timeout=15.0)
        try:
            target = sqlite3.connect(temp_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        with open(temp_path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[CLOUD SYNC] ⚠️ Could not snapshot the database: {e}", flush=True)
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def get_db_hash() -> str:
    """SHA-256 of the local database file ('' when missing)."""
    if not os.path.exists(db_path()):
        return ""
    with open(db_path(), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _looks_like_sqlite(raw: bytes) -> bool:
    """True when the bytes begin with SQLite's file header.

    Guards the restore path: downloading something that isn't a database (a
    stray upload, a truncated transfer) must never replace the live bot.db.
    """
    return bool(raw) and raw[:16] == SQLITE_MAGIC


def _db_looks_empty(path: str | None = None) -> bool:
    """True when no member has any level or economy row.

    A deleted local database still gets recreated empty by ``db.setup()``. If
    that empty file were uploaded it would replace everybody's real progress,
    so the upload path refuses when the remote already holds a backup.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(path or db_path(), timeout=5.0)
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
        # Can't tell — never block on an unknown, that would stop all backups.
        return False


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def _snapshot_names(files: dict) -> list[str]:
    return sorted(
        (name for name in files if name.startswith(SNAPSHOT_PREFIX) and name.endswith(".enc")),
        reverse=True,  # timestamp names sort newest-first
    )


def _decode_candidate(name: str, content: str, key: bytes | None) -> bytes | None:
    """Turn one Gist file's text into plaintext database bytes, or None."""
    if not content:
        return None
    try:
        blob = base64.b64decode(content)
    except Exception:
        return None
    if name.endswith(".enc"):
        if key is None:
            return None
        return decrypt_bytes(blob, key)
    # Legacy plaintext payload.
    return blob


def restore_from_cloud() -> bool:
    """Fetch the newest usable snapshot from the Gist on startup."""
    global _last_sync_hash
    if not GITHUB_TOKEN or not GIST_ID:
        print("[CLOUD SYNC] ⚠️ Missing GITHUB_BACKUP_TOKEN or GIST_BACKUP_ID. Skipping cloud restore.", flush=True)
        return False

    data = _gist_get()
    if data is None:
        return False

    if data.get("public"):
        print(
            "[CLOUD SYNC] 🔓 WARNING: the backup Gist is PUBLIC — anyone with its URL "
            "can read the uploaded file(s). Make it secret.",
            flush=True,
        )

    files = data.get("files", {}) or {}
    key = encryption_key()

    candidates: list[str] = []
    if CURRENT_FILE in files:
        candidates.append(CURRENT_FILE)
    candidates.extend(_snapshot_names(files))
    if LEGACY_PLAINTEXT_FILE in files:
        candidates.append(LEGACY_PLAINTEXT_FILE)

    if not candidates:
        print("[CLOUD SYNC] ⚠️ No backup file found in the Gist.", flush=True)
        return False

    for name in candidates:
        content = (files.get(name) or {}).get("content", "")
        try:
            db_bytes = _decode_candidate(name, content, key)
        except Exception as e:
            print(f"[CLOUD SYNC] ⚠️ Snapshot '{name}' could not be used: {e}", flush=True)
            continue
        if not _looks_like_sqlite(db_bytes):
            print(
                f"[CLOUD SYNC] ⚠️ Snapshot '{name}' is not a valid SQLite database — "
                "refusing to overwrite the local one with it.",
                flush=True,
            )
            continue

        temp_path = f"{db_path()}.tmp"
        with open(temp_path, "wb") as f:
            f.write(db_bytes)
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            # A stale write-ahead log left over from the previous database would
            # be replayed over the restored file and silently discard it — the
            # download would succeed while every row disappeared. Remove it (and
            # its shared-memory index) before swapping the file in.
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(db_path() + suffix)
                except FileNotFoundError:
                    pass
            os.replace(temp_path, db_path())
            _last_sync_hash = hashlib.sha256(db_bytes).hexdigest()
            encrypted = "encrypted" if name.endswith(".enc") else "plaintext"
            print(
                f"[CLOUD SYNC] ✅ Restored database from '{name}' ({encrypted}, "
                f"{len(db_bytes):,} bytes). Zero progress lost.",
                flush=True,
            )
            return True

    print("[CLOUD SYNC] ❌ No usable snapshot could be restored.", flush=True)
    return False


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
def backup_to_cloud_sync() -> bool:
    """Upload the current bot.db to the Gist, encrypted when a key is set.

    Two safety gates protect real progress from being overwritten: the upload is
    refused when the remote holds encrypted backups we have no key for, and when
    the local database is empty while the remote already holds a backup (the
    signature of a failed restore). A backup job that silently replaces good
    data is worse than one that does nothing, so both fail closed.
    """
    global _last_sync_hash, _last_snapshot_ts, _remote_checked

    if not os.path.exists(db_path()) or os.path.getsize(db_path()) == 0:
        return False
    if not GITHUB_TOKEN or not GIST_ID:
        return False

    raw_bytes = snapshot_db_bytes()
    if not raw_bytes:
        return False
    current_hash = hashlib.sha256(raw_bytes).hexdigest()
    if current_hash == _last_sync_hash:
        return True  # Nothing changed since the last upload.

    key = encryption_key()
    snapshot_due = (time.time() - _last_snapshot_ts) >= SNAPSHOT_INTERVAL_SECONDS

    # Inspect the remote before the first upload (and whenever a snapshot is
    # due), because this is the window in which a bad deploy could clobber a
    # good backup.
    remote_files = None
    if not _remote_checked or snapshot_due:
        gist = _gist_get()
        if gist is not None:
            remote_files = gist.get("files", {}) or {}
            _remote_checked = True

    if remote_files:
        if any(name.endswith(".enc") for name in remote_files) and not key:
            print(
                "[CLOUD SYNC] 🛑 Refusing to upload: the Gist holds ENCRYPTED backups but "
                "BACKUP_ENCRYPTION_KEY is not set. Restore the key (or fix the Gist) before "
                "writing, so a good backup is never overwritten.",
                flush=True,
            )
            return False
        if _db_looks_empty():
            print(
                "[CLOUD SYNC] 🛑 Refusing to upload: the local database has no level or "
                "economy rows, but the Gist already holds a backup. That means the restore "
                "did not happen — uploading now would replace real progress with an empty "
                "database.",
                flush=True,
            )
            return False
    elif not _remote_checked and _db_looks_empty():
        # Couldn't reach the Gist and there is nothing worth backing up anyway.
        # Wait for a successful check rather than risk the same overwrite.
        print(
            "[CLOUD SYNC] ⏳ Could not verify the remote before an empty local database; "
            "deferring this upload until the Gist can be read.",
            flush=True,
        )
        return False

    payload_files: dict[str, dict | None] = {}

    if key:
        payload_files[CURRENT_FILE] = {
            "content": base64.b64encode(encrypt_bytes(raw_bytes, key)).decode("ascii")
        }
        if remote_files and LEGACY_PLAINTEXT_FILE in remote_files:
            # Remove the old plaintext copy the moment encryption is on.
            payload_files[LEGACY_PLAINTEXT_FILE] = None

        if snapshot_due:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            snapshot_name = f"{SNAPSHOT_PREFIX}{stamp}.enc"
            payload_files[snapshot_name] = {
                "content": base64.b64encode(encrypt_bytes(raw_bytes, key)).decode("ascii")
            }
            if remote_files is not None:
                names = _snapshot_names(remote_files)
                names.append(snapshot_name)
                names.sort(reverse=True)
                for stale in names[BACKUP_KEEP:]:
                    payload_files[stale] = None
            _last_snapshot_ts = time.time()
        description = f"Meow Bot encrypted DB snapshot ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})"
    else:
        print(
            "[CLOUD SYNC] ⚠️ BACKUP_ENCRYPTION_KEY is not set — uploading the database in "
            "PLAINTEXT. Anyone who can read the Gist can read every wallet and warning. "
            "Set a key to encrypt it.",
            flush=True,
        )
        payload_files[LEGACY_PLAINTEXT_FILE] = {
            "content": base64.b64encode(raw_bytes).decode("ascii")
        }
        description = f"Meow Discord Bot Persistent Cloud SQLite Database (Auto-saved at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})"

    ok = _gist_patch({"description": description, "files": payload_files})
    if ok:
        _last_sync_hash = current_hash
        print(
            f"[CLOUD SYNC] ☁️ Database synced ({'encrypted' if key else 'plaintext'}, "
            f"{len(raw_bytes):,} bytes).",
            flush=True,
        )
    return ok


async def cloud_sync_loop():
    """Background task syncing database changes to the cloud every 25 seconds."""
    while True:
        try:
            await asyncio.sleep(25)
            await asyncio.to_thread(backup_to_cloud_sync)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[CLOUD SYNC LOOP] ⚠️ Loop error: {e}", flush=True)


def start_cloud_sync_loop(bot):
    """Start the asynchronous auto-sync background loop."""
    if not GITHUB_TOKEN or not GIST_ID:
        print("[CLOUD SYNC] ⏸️ Auto-sync loop not started (no credentials).", flush=True)
        return None
    return bot.loop.create_task(cloud_sync_loop())
