"""
Persistent Cloud Database Sync for Meow Discord Bot.

Preserves the SQLite database (bot.db) across cloud deploys, restarts and sleep
cycles using GitHub Gists, so levels, economy, inventory, tickets and module
toggles survive.

Security notes
--------------
* Credentials come from the environment only. Never hardcode a token here —
  this file is public and a committed token is compromised forever.
* The database is encrypted with PyNaCl SecretBox (XSalsa20-Poly1305) *before*
  it leaves the machine, so a leaked Gist URL exposes nothing. Plaintext is
  never written. The key is ``BACKUP_ENCRYPTION_KEY`` when configured, and
  otherwise one derived from ``GITHUB_BACKUP_TOKEN`` — a value that never
  reaches the Gist — so encryption is on by default and does not depend on an
  operator remembering to configure it. Rotating the token (or the explicit
  key) changes the key, so uploads are refused while a backup exists that this
  bot can no longer read; that only copy of everyone's progress is never
  overwritten.
* Every payload carries a SHA-256 of the plaintext, verified after decryption,
  so a truncated or tampered snapshot is refused rather than restored.
* Rotated snapshots (``bot-db-<timestamp>.enc``) allow point-in-time restore;
  the newest ``BACKUP_KEEP`` are retained.
"""
import os
import time
import hmac
import base64
import asyncio
import hashlib
import tempfile
import threading
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

# Domain separation for the key derived from the backup token, so the token is
# never used as a key directly and this derivation can't collide with any other
# use of the same token.
KEY_DERIVATION_LABEL = b"meow-bot-backup-key-v1"

_last_sync_hash = None
_last_snapshot_ts = 0.0
# Whether we have successfully listed the remote files this process. The first
# upload after a deploy is the dangerous one, so the remote is inspected then.
_remote_checked = False
# Set when a restore found encrypted backups that this bot could not read back.
# Uploads stay blocked while it is set, so the unreadable (but intact) backup is
# never replaced by a fresh one.
_restore_blocked_reason: str | None = None
# Only one upload may run at a time: the dashboard schedules an immediate backup
# in its own thread, and two concurrent uploads share the "what did I last
# upload" state and can prune a snapshot the other one just wrote.
_upload_lock = threading.Lock()
# Operators only need to read each warning once, not every 25 seconds.
_warned: set[str] = set()


def _warn_once(message: str, flush: bool = True) -> None:
    """Print ``message`` the first time it is seen in this process."""
    if message in _warned:
        return
    _warned.add(message)
    print(message, flush=flush)


def _block_restores(reason: str) -> None:
    """Record why nothing may be uploaded, and say so once.

    Used when this process started without the state that is already stored in the
    Gist. Whatever else it does, it must not be the process that replaces that
    state with its own — a newer, emptier database uploading over a good backup
    destroys progress permanently.
    """
    global _restore_blocked_reason
    _restore_blocked_reason = reason
    _warn_once(
        f"[CLOUD SYNC] 🛑 {reason} Automatic uploads are disabled for this process, so the "
        "backup already in the Gist cannot be replaced by a database that does not "
        "contain it. Fix the cause and restart."
    )


# --------------------------------------------------------------------------- #
# Crypto helpers (pure functions — unit-testable without any network access)
# --------------------------------------------------------------------------- #
def explicit_encryption_key() -> bytes | None:
    """Decode BACKUP_ENCRYPTION_KEY (base64 of 32 raw bytes), or None.

    An unusable value is reported once and then treated as absent: the caller
    falls back to the token-derived key rather than to plaintext.
    """
    raw = (os.environ.get("BACKUP_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        _warn_once(
            "[CLOUD SYNC] ❌ BACKUP_ENCRYPTION_KEY is not valid base64 — ignoring it and "
            "using the key derived from GITHUB_BACKUP_TOKEN instead."
        )
        return None
    if len(key) != 32:
        _warn_once(
            f"[CLOUD SYNC] ❌ BACKUP_ENCRYPTION_KEY must decode to 32 bytes (got {len(key)}) "
            "— ignoring it and using the key derived from GITHUB_BACKUP_TOKEN instead."
        )
        return None
    return key


def derive_key_from_token(token: str) -> bytes:
    """The 32-byte key a given backup token derives. Pure, so it is testable."""
    return hmac.new(token.strip().encode("utf-8"), KEY_DERIVATION_LABEL, hashlib.sha256).digest()


def base64_key_for_token(token: str) -> str:
    """Operator helper: the ``BACKUP_ENCRYPTION_KEY`` value a token derives.

    Rotating GITHUB_BACKUP_TOKEN otherwise strands every snapshot written under
    it. Pasting this value into BACKUP_ENCRYPTION_KEY makes those snapshots
    readable again, so the old chain can still be restored from.
    """
    return base64.b64encode(derive_key_from_token(token)).decode("ascii")


def derived_encryption_key() -> bytes | None:
    """A stable key derived from the backup token, or None without one.

    The token is a secret that is never stored in the Gist, so encrypting with a
    key derived from it means a leaked (or accidentally public) Gist exposes
    nothing — while requiring no extra configuration. A dedicated
    BACKUP_ENCRYPTION_KEY still takes precedence when it is set.
    """
    token = (GITHUB_TOKEN or "").strip()
    if not token:
        return None
    return derive_key_from_token(token)


def encryption_key() -> bytes | None:
    """The key new uploads are encrypted with.

    The explicit key wins; otherwise the key derived from the backup token is
    used. It is None only when cloud backup is configured with neither, which
    the upload path treats as "never write anything".
    """
    explicit = explicit_encryption_key()
    if explicit is not None:
        return explicit
    derived = derived_encryption_key()
    if derived is not None:
        _warn_once(
            "[CLOUD SYNC] 🔐 BACKUP_ENCRYPTION_KEY is not set — encrypting backups with a "
            "key derived from GITHUB_BACKUP_TOKEN. The Gist holds nothing readable without "
            "that token. Set BACKUP_ENCRYPTION_KEY to a dedicated key to rotate it "
            "independently; existing snapshots stay restorable."
        )
    return derived


def decryption_keys() -> list[bytes]:
    """Every key that may have written the stored backups, most likely first.

    Keeping the derived key in the list means turning on a dedicated
    BACKUP_ENCRYPTION_KEY later does not strand the snapshots written before it.
    """
    keys: list[bytes] = []
    for candidate in (explicit_encryption_key(), derived_encryption_key()):
        if candidate is not None and candidate not in keys:
            keys.append(candidate)
    return keys


def key_id(key: bytes | None) -> str:
    """Short non-reversible fingerprint, so logs can name the key in use."""
    if not key:
        return "none"
    return hashlib.sha256(key).hexdigest()[:8]


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


def _decode_candidate(
    name: str, content: str, keys: list[bytes] | bytes | None
) -> bytes | None:
    """Turn one Gist file's text into plaintext database bytes, or None.

    Encrypted payloads are tried against every key this bot holds, so a snapshot
    written before a key change still restores.
    """
    if not content:
        return None
    try:
        blob = base64.b64decode(content)
    except Exception:
        return None
    if name.endswith(".enc"):
        if isinstance(keys, (bytes, bytearray)):
            keys = [bytes(keys)]
        for key in keys or ():
            try:
                return decrypt_bytes(blob, key)
            except Exception:
                continue
        return None
    # Legacy plaintext payload — still readable, never written again.
    return blob


def restore_from_cloud() -> bool:
    """Fetch the newest usable snapshot from the Gist on startup."""
    global _last_sync_hash, _restore_blocked_reason
    if not GITHUB_TOKEN or not GIST_ID:
        print("[CLOUD SYNC] ⚠️ Missing GITHUB_BACKUP_TOKEN or GIST_BACKUP_ID. Skipping cloud restore.", flush=True)
        return False
    # A fresh attempt decides this from scratch.
    _restore_blocked_reason = None

    data = _gist_get()
    if data is None:
        # The Gist exists but could not be read: this process has no idea what it
        # is running without. Backing up from here would overwrite a good backup
        # with whatever this database happens to hold.
        _block_restores(
            "the startup restore could not read the backup Gist, so this process may be "
            "running without everyone's progress."
        )
        return False

    if data.get("public"):
        print(
            "[CLOUD SYNC] 🔓 WARNING: the backup Gist is PUBLIC — anyone with its URL "
            "can read the uploaded file(s). Make it secret.",
            flush=True,
        )

    files = data.get("files", {}) or {}
    keys = decryption_keys()

    candidates: list[str] = []
    if CURRENT_FILE in files:
        candidates.append(CURRENT_FILE)
    candidates.extend(_snapshot_names(files))
    if LEGACY_PLAINTEXT_FILE in files:
        candidates.append(LEGACY_PLAINTEXT_FILE)

    if not candidates:
        print("[CLOUD SYNC] ⚠️ No backup file found in the Gist.", flush=True)
        return False

    # Encrypted payloads this bot could not read back. If nothing else restores,
    # they are the reason uploads must stop: the data is still in there, just
    # written with a key we no longer hold.
    unreadable_encrypted = 0

    for name in candidates:
        content = (files.get(name) or {}).get("content", "")
        try:
            db_bytes = _decode_candidate(name, content, keys)
        except Exception as e:
            print(f"[CLOUD SYNC] ⚠️ Snapshot '{name}' could not be used: {e}", flush=True)
            continue
        if db_bytes is None and name.endswith(".enc"):
            unreadable_encrypted += 1
            print(
                f"[CLOUD SYNC] ⚠️ Snapshot '{name}' could not be decrypted with any of the "
                f"{len(keys)} key(s) available to this bot — it was written with a "
                "different BACKUP_ENCRYPTION_KEY or a since-rotated GITHUB_BACKUP_TOKEN.",
                flush=True,
            )
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

    if unreadable_encrypted:
        _block_restores(
            f"the restore found {unreadable_encrypted} encrypted snapshot(s) in the Gist "
            "that none of the available key(s) could decrypt (usually a changed "
            "BACKUP_ENCRYPTION_KEY or a rotated GITHUB_BACKUP_TOKEN)."
        )

    print("[CLOUD SYNC] ❌ No usable snapshot could be restored.", flush=True)
    return False


def restore_blocked_reason() -> str | None:
    """Why uploads are paused, or None when nothing is blocking them."""
    return _restore_blocked_reason


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
def backup_to_cloud_sync() -> bool:
    """Upload an encrypted snapshot of the current bot.db to the Gist.

    Plaintext is never written. ``_backup_locked`` applies the safety gates; this
    wrapper only ensures one upload runs at a time, because the dashboard
    schedules an immediate backup from its own thread and two concurrent uploads
    share the "what did I last upload" state and can prune a snapshot the other
    one just wrote.
    """
    if not _upload_lock.acquire(blocking=False):
        return False
    try:
        return _backup_locked()
    finally:
        _upload_lock.release()


def _backup_locked() -> bool:
    """The upload itself. Callers must hold ``_upload_lock``.

    Five gates protect real progress from being overwritten, because a backup job
    that silently replaces good data is worse than one that does nothing:

    * the upload is refused while a restore could not read what is already stored
      (a changed key, an unreachable Gist) — that copy stays intact;
    * it is refused when the local database is empty while the remote already
      holds a backup (the signature of a failed restore);
    * it is deferred until the remote has actually been read once this process,
      so the first upload after a deploy can never write over a backup it never
      looked at;
    * it is refused when the Gist holds encrypted backups no available key can
      read back;
    * it is refused outright when no encryption key is available at all.
    """
    global _last_sync_hash, _last_snapshot_ts, _remote_checked

    if not os.path.exists(db_path()) or os.path.getsize(db_path()) == 0:
        return False
    if not GITHUB_TOKEN or not GIST_ID:
        return False

    if _restore_blocked_reason:
        _warn_once(
            f"[CLOUD SYNC] 🛑 Refusing to upload: {_restore_blocked_reason} Uploading would "
            "replace the backup already in the Gist — possibly the only copy of everyone's "
            "progress — with this process's database. Fix the cause and restart. If a key "
            "was rotated on purpose, delete the old snapshot file(s) from the Gist first, "
            "so starting a new chain is a deliberate act."
        )
        return False

    raw_bytes = snapshot_db_bytes()
    if not raw_bytes:
        return False
    current_hash = hashlib.sha256(raw_bytes).hexdigest()
    if current_hash == _last_sync_hash:
        return True  # Nothing changed since the last upload.

    key = encryption_key()
    if key is None:
        _warn_once(
            "[CLOUD SYNC] 🛑 Refusing to upload: no encryption key is available. Backups "
            "are never written in plaintext; set GITHUB_BACKUP_TOKEN or "
            "BACKUP_ENCRYPTION_KEY to enable them."
        )
        return False

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
        elif not _remote_checked:
            # Never write to a Gist we have not read: an unread remote may hold a
            # backup that this upload would silently replace.
            _warn_once(
                "[CLOUD SYNC] ⏳ Deferring this upload: the backup Gist could not be read, "
                "so there is no way to know what it already holds. Retrying next cycle."
            )
            return False

    if remote_files:
        if any(name.endswith(".enc") for name in remote_files) and not decryption_keys():
            _warn_once(
                "[CLOUD SYNC] 🛑 Refusing to upload: the Gist holds ENCRYPTED backups and no "
                "key is available to read them back. Set BACKUP_ENCRYPTION_KEY (or restore "
                "GITHUB_BACKUP_TOKEN) before writing, so a good backup is never "
                "overwritten."
            )
            return False
        if _db_looks_empty():
            _warn_once(
                "[CLOUD SYNC] 🛑 Refusing to upload: the local database has no level or "
                "economy rows, but the Gist already holds a backup. That means the restore "
                "did not happen — uploading now would replace real progress with an empty "
                "database."
            )
            return False

    payload_files: dict[str, dict | None] = {
        CURRENT_FILE: {
            "content": base64.b64encode(encrypt_bytes(raw_bytes, key)).decode("ascii")
        }
    }
    if remote_files and LEGACY_PLAINTEXT_FILE in remote_files:
        # Retire the plaintext copy the moment an encrypted one exists.
        payload_files[LEGACY_PLAINTEXT_FILE] = None

    snapshot_name = None
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

    ok = _gist_patch({
        "description": f"Meow Bot encrypted DB snapshot ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})",
        "files": payload_files,
    })
    if not ok:
        # Leave _last_sync_hash alone so the next cycle retries, and don't claim a
        # snapshot that was never stored.
        return False

    _last_sync_hash = current_hash
    if snapshot_name:
        _last_snapshot_ts = time.time()
    print(
        f"[CLOUD SYNC] ☁️ Database synced (encrypted, key {key_id(key)}, "
        f"{len(raw_bytes):,} bytes).",
        flush=True,
    )
    return True


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
