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

import gzip
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
from dataclasses import dataclass

from utils import config_audit

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUPABASE_URL: str = config_audit.get("SUPABASE_URL").rstrip("/")
SUPABASE_KEY: str = config_audit.get("SUPABASE_SERVICE_KEY")
BUCKET: str = "bot-backups"

#: The primary backup file — always the latest state.
CURRENT_FILE: str = "bot.db"
#: Snapshots for point-in-time restore, named ``bot-db-YYYYMMDD-HHMMSS.db``.
SNAPSHOT_PREFIX: str = "bot-db-"
SNAPSHOT_SUFFIX: str = ".db"
#: How many snapshots to keep (oldest pruned first).
try:
    BACKUP_KEEP: int = max(1, int(config_audit.get("BACKUP_KEEP", "5")))
except (TypeError, ValueError):
    BACKUP_KEEP = 5
#: Seconds between snapshot rotations.
SNAPSHOT_INTERVAL: int = 3600  # 1 hour
#: Payloads at least this big travel gzipped. Below it the envelope costs more
#: than it saves (the lease is a paragraph of JSON).
PACK_MIN_BYTES: int = 1024
#: What gzip writes in its first two bytes, and how a download tells a packed
#: object from the raw one written by every release before this one.
_GZIP_MAGIC: bytes = b"\x1f\x8b"
#: The level used for a whole database. The snapshot is read at most once a
#: tick and the upload is a moment against a 25-second budget, so the last few
#: percent of ratio are worth more than the milliseconds level 6 would save.
PACK_LEVEL: int = 6
#: Seconds between periodic backup checks (default 5m to protect bandwidth quotas).
try:
    SYNC_INTERVAL: int = max(10, int(config_audit.get("CLOUD_SYNC_INTERVAL", "300")))
except (TypeError, ValueError):
    SYNC_INTERVAL = 300

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
            if not isinstance(payload, list):
                return None
        _touch_contact()
        return payload
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

    The bytes are unpacked here rather than at each caller so every object this
    module ever reads answers with what was written — see :func:`_unpack_blob`.
    """
    try:
        req = urllib.request.Request(
            _storage_url(name),
            headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _unpack_blob(resp.read())
        _touch_contact()
        return data
    except urllib.error.HTTPError as exc:
        if quiet and exc.code in (400, 404):
            # A 404 is still the target answering: the object is not there, which
            # is an answer, and proof that the thing that holds the backup is up.
            _touch_contact()
            return b""
        if not quiet:
            print(f"[CLOUD SYNC] ⚠️ Supabase download '{name}' failed: {exc}", flush=True)
        return None
    except Exception as exc:
        if not quiet:
            print(f"[CLOUD SYNC] ⚠️ Supabase download '{name}' failed: {exc}", flush=True)
        return None


def _pack_blob(data: bytes) -> bytes:
    """gzip a payload before it is sent, when that is worth doing.

    A database is the one thing this bot uploads, and it is uploaded over and
    over: the whole of ``bot.db`` goes to Supabase on every change, plus an
    hourly snapshot. It is mostly SQLite pages of JSON, numbers and names, so it
    compresses to roughly a third of its size — and the bytes saved are bytes
    that would otherwise be counted against this host's egress allowance, which
    is the allowance that ran out.

    A payload that does not shrink is sent exactly as it is. Random bytes gzip
    *larger*, and an incompressible megabyte is not made cheaper by wrapping it.
    """
    if len(data) < PACK_MIN_BYTES:
        return data
    try:
        packed = gzip.compress(data, PACK_LEVEL)
    except Exception as exc:
        _warn_once(f"[CLOUD SYNC] ⚠️ Could not compress a {len(data):,}-byte payload: {exc}")
        return data
    return packed if len(packed) < len(data) else data


def _unpack_blob(blob: bytes) -> bytes:
    """What was uploaded, whether or not it went up packed.

    The check is the gzip signature rather than a flag, so the bucket needs no
    migration and no second object: a ``bot.db`` written by the release before
    this one is still a raw SQLite file, is still recognised as one, and still
    restores. A corrupt packed object is handed back as it arrived, which the
    restore path already knows how to refuse — the alternative, raising here,
    would fail the whole restore over one unreadable candidate.
    """
    if not blob.startswith(_GZIP_MAGIC):
        return blob
    try:
        return gzip.decompress(blob)
    except Exception as exc:
        print(f"[CLOUD SYNC] ⚠️ A stored object says it is gzipped and is not: {exc}", flush=True)
        return blob


def _storage_upload(name: str, data: bytes) -> bool:
    """Upload (or overwrite) a file in the bucket.

    Uses PUT (not POST) because Supabase Storage's POST endpoint returns
    409 "The resource already exists" when the file is already in the bucket.
    PUT replaces it silently.

    The payload is packed in here, at the one door every upload goes through, so
    the snapshots are covered by the same rule as the current file and no caller
    has to remember it. The object's name does not change: a packed ``bot.db``
    is still ``bot.db``, and every reader unpacks it transparently.
    """
    sent = _pack_blob(data)
    try:
        req = urllib.request.Request(
            _storage_url(name),
            data=sent,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/octet-stream",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            ok = resp.status in (200, 201)
        if ok:
            _touch_contact()
        if ok and len(sent) != len(data):
            print(
                f"[CLOUD SYNC] 🗜️ '{name}': {len(data):,} bytes sent as "
                f"{len(sent):,} ({len(data) / len(sent):.1f}x smaller).",
                flush=True,
            )
        return ok
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
#: The floor between two lease refreshes (seconds) — the fastest an idle instance
#: *may* renew its claim, not how often it does: the refresh happens inside the
#: backup tick, so :data:`SYNC_INTERVAL` is the cadence a boot can rely on.
LEASE_TOUCH_INTERVAL: int = 120
#: How long a boot listens before it believes the lease names a copy that is
#: *answering*. It has to outlast **:data:`SYNC_INTERVAL`**, because that is how
#: often a running copy renews its claim — the refresh lives inside the backup
#: tick, and a watch shorter than one tick reads a copy that is answering as a
#: claim left behind. Observed live: a Space container's claim was 114s old when
#: the watch began and was never written again inside the 150s window it used to
#: be, so a *running* copy was answered "not a copy that is answering" and no card
#: was sent. A copy that has stopped leaves its claim in the bucket for the whole
#: of :data:`LEASE_TTL`, so a longer watch still filters a corpse out: the second
#: look has to find the claim fresh *and* moved.
COPY_WATCH_SECONDS: int = SYNC_INTERVAL + 30
#: Consecutive checks a *shrunk* database is held back before its state is accepted.
SHRINK_GRACE_TICKS: int = 4
#: Escape hatch for a deliberate wipe: ``CLOUD_SYNC_ALLOW_SHRINK=1``.
ALLOW_SHRINK: bool = config_audit.flag("CLOUD_SYNC_ALLOW_SHRINK")

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
#: How many consecutive upload attempts have failed.
_consecutive_upload_failures: int = 0
#: When the object this process restored was installed, so a reader can age it.
_restored_at: float = 0.0
#: When the bucket last *answered this process* — a listing that came back, an
#: object that downloaded, a write that landed. The heartbeat the state reading
#: is built on, and the only figure here that notices a dead target on a quiet
#: server: an idle database uploads nothing, so it never fails at anything, and
#: a target that has stopped answering is otherwise completely silent.
_last_contact_ts: float = 0.0
#: The last answer :func:`upload_refusal_reason` gave the upload path. Recorded
#: rather than re-asked, so a health check reports the block instead of spending
#: a decision — and, on an endpoint a monitor polls, instead of a network call.
_last_refusal_reason: str | None = None

_warned: set[str] = set()


def _touch_contact() -> None:
    """Note that the bucket answered. Called at the one place each call returns."""
    global _last_contact_ts
    _last_contact_ts = time.time()


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

        # Verify SQLite file integrity before trusting it
        try:
            conn = sqlite3.connect(f"file:{temp}?mode=ro", uri=True, timeout=5.0)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    print(
                        f"[CLOUD SYNC] ⚠️ '{name}' failed PRAGMA integrity_check: {row} — skipping.",
                        flush=True,
                    )
                    _remove(temp)
                    continue
            finally:
                conn.close()
        except Exception as exc:
            print(
                f"[CLOUD SYNC] ⚠️ '{name}' failed integrity check: {exc} — skipping.",
                flush=True,
            )
            _remove(temp)
            continue

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
    global _last_sync_hash, _restored_from, _restored_counts, _restored_at

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
    _restored_at = time.time()
    _touch_contact()
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


#: How long the bucket may stay silent before the backup is called *not
#: answering* rather than merely idle. Two full ticks: the loop speaks to the
#: bucket every :data:`SYNC_INTERVAL` even when nothing changed — that is what
#: the lease refresh is for — so two missed ticks is not a slow network, it is a
#: target that has stopped answering. A backup nobody notices has stopped is the
#: exact failure this reading exists to make loud.
SILENT_AFTER: int = 2 * SYNC_INTERVAL


def backup_state(now: float | None = None) -> dict:
    """What this process *already knows* about the backup, as one small mapping.

    Read, never asked: no request, no write, no grace tick spent. That is the
    point rather than an optimisation — this is rendered by ``/health``, which a
    monitor may poll every minute, and a health check that reaches the network is
    a health check that can hang while reporting on whether something else hangs.

    The two questions it answers are the two that had no answer anywhere a human
    looks: **when** the backup last did anything, and **why** it is not doing
    anything now. A backup that has quietly stopped has no symptom at all — the
    bot runs, the pages render, and the only copy of everybody's progress is
    weeks old — so the last successful upload, the last successful restore and
    the refusal sentence are stated here rather than left in a host log.

    ``status`` is one word, and it is about the *target* rather than about the
    file, because that is the question a monitor is asking:

    * ``unconfigured`` — no Supabase URL or key, so there is no backup at all.
    * ``blocked`` — a decision not to upload, with ``refusal`` holding why.
    * ``failing`` — uploads have been *attempted* and are not landing.
    * ``silent`` — the bucket answered before and has stopped answering. The one
      state that a quiet server reaches, and the reason the reading tracks the
      last *answer* rather than the last upload: an unchanged database uploads
      nothing, so "no upload for an hour" is normal while "no answer for an
      hour" is a dead backup target.
    * ``unreachable`` — the bucket has been asked in this process and has never
      answered: the listing or the download never came back. Separate from
      ``unknown`` on purpose, and this is the distinction the whole reading is
      for — *we have asked and heard nothing* used to read exactly like *nobody
      has asked yet*, which is the shape of a machine that is quietly saving
      nothing while its monitor stays green. (It was found by reading the
      deployed answer rather than by reasoning: the Space's own ``/health``
      served ``unknown`` while its bucket was refusing to answer.)
    * ``waiting`` — the target answers and nothing has been saved **by this
      process** yet. A restore counts as an answer, so a boot that picked the
      backup up reads this until an upload of its own lands — which, on a server
      whose database has not changed since, is the right answer rather than a
      problem: there is nothing new to save.
    * ``ok`` — the target answered recently and a backup of this process's own
      has landed.
    * ``unknown`` — nothing has been asked of the bucket in this process, and
      nothing may be claimed about it either way.
    """
    moment = time.time() if now is None else float(now)
    configured = bool(SUPABASE_URL and SUPABASE_KEY)
    refusal = _restore_blocked_reason or _last_refusal_reason
    contact_age = (moment - _last_contact_ts) if _last_contact_ts else None
    if not configured:
        word = "unconfigured"
    elif refusal:
        word = "blocked"
    elif _consecutive_upload_failures:
        word = "failing"
    elif contact_age is not None and contact_age > SILENT_AFTER:
        word = "silent"
    elif contact_age is None:
        # Asked and no answer is not the same claim as never asked. `checked` is
        # set by the attempt itself, before the call that may fail.
        word = "unreachable" if _remote_checked else "unknown"
    elif _last_upload_ts:
        word = "ok"
    else:
        word = "waiting"
    return {
        "status": word,
        "configured": configured,
        # Whether the bucket has been looked at at all — the difference between
        # an unasked question and a clean answer.
        "checked": bool(_remote_checked),
        "refusal": refusal,
        "failures": int(_consecutive_upload_failures),
        # Ages rather than stamps, like the rest of ``/health``: a monitor that
        # has to subtract two clocks is a monitor that will get it wrong.
        "uploaded_seconds_ago": _age(_last_upload_ts, moment),
        "contacted_seconds_ago": None if contact_age is None else max(0, int(contact_age)),
        "restored_from": _restored_from,
        "restored_rows": _total_rows(_restored_counts) if _restored_from else 0,
        "restored_seconds_ago": _age(_restored_at, moment),
    }


def _age(stamp: float, moment: float) -> int | None:
    return max(0, int(moment - stamp)) if stamp else None


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

_upload_lock = threading.Lock()
_upload_requested = False


def _snapshot_bytes(path: str) -> bytes | None:
    """A consistent copy of the database, WAL included — and compacted.

    Consistent first, because it is the whole point. Reading the file directly
    is not enough in WAL mode: every row committed since the last checkpoint
    lives in ``bot.db-wal``, so a raw read uploads a database that is *missing
    the newest progress* — and the next restart restores that older state, which
    is exactly what ""my coins went back"" looks like.

    Compacted as well, because this copy is not a file on a disk — it is the
    payload of an upload that happens after every change, and its size is money.
    ``VACUUM INTO`` writes a database holding exactly the rows that are there,
    while the backup API copies the file page for page *including the pages a
    deleted row left behind*. Those free pages would travel in every backup
    forever: a table that prunes itself — the market's candles, the queue's
    finished jobs, the test fixtures removed at boot — would keep costing its
    old size, because nothing shrinks a file it is only ever copied.

    ``VACUUM INTO`` is the same read-transaction guarantee as the backup API
    (SQLite's own documentation offers it as the way to copy a live database),
    it is byte-for-byte repeatable — which the change detection depends on, and
    which was verified rather than assumed — and it *shrinks*. The provider is
    not on every supported SQLite, so a host that cannot do it falls back to the
    page-for-page copy rather than losing its backups.
    """
    fd, temp = tempfile.mkstemp(prefix="meow-snapshot-", suffix=".db")
    os.close(fd)
    # ``VACUUM INTO`` refuses a target that already exists, and ``mkstemp``
    # exists to have created one.
    _remove(temp)
    try:
        source = sqlite3.connect(path, timeout=15.0)
        try:
            source.execute("VACUUM INTO ?", (temp,))
        finally:
            source.close()
        with open(temp, "rb") as f:
            return f.read()
    except Exception as exc:
        print(
            f"[CLOUD SYNC] ⚠️ Could not compact the snapshot ({exc}); "
            "copying the file as it is.",
            flush=True,
        )
        return _snapshot_bytes_uncompacted(path)
    finally:
        _remove(temp)


def _snapshot_bytes_uncompacted(path: str) -> bytes | None:
    """SQLite's backup API: consistent, complete, and the size of the file."""
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

@dataclass(frozen=True)
class Reading:
    """The writer slot, read as one of the three answers either caller acts on.

    Two callers ask this one question — the backup (*may I write?*) and the boot
    (*is another copy answering Discord?*) — and between them they need three
    answers: somebody else is live, nobody else is, or there is no reading to
    decide from. The six readings this used to keep were six names for those
    three, and each name carried a sentence of its own in the log: a boot that
    found nobody, a boot whose bucket would not answer and a boot whose check
    never ran were one silence, while *nobody* got three ways of saying so beside
    the one answer worth reading.

    Collapsing them loses nothing either caller uses. *This is my own claim*,
    *there is no claim* and *the claim is somebody's but has stopped being
    renewed* are the same fact — nobody else is holding the slot — and the one
    distinction that matters between them, the name left behind, is printed by
    :func:`_read_slot` itself when it takes the slot over.

    What the answers are made of is kept: the name in the record, and when it was
    written — raw rather than aged, because the boot's second look asks whether
    the claim *moved*, which an age cannot say.
    """

    #: Another copy holds the slot and renewed it inside :data:`LEASE_TTL`.
    LIVE = "live"
    #: Nobody else is holding it: this machine's own claim, no claim at all, or
    #: one left behind by a copy that stopped renewing it.
    FREE = "free"
    #: No usable reading, which is not a fact about the bucket at all: the record
    #: will not parse, nothing was asked here, or the call itself failed.
    UNKNOWN = "unknown"

    state: str
    #: The name the record was written under, when there is one.
    holder: str | None = None
    #: When it was written, on the writer's own clock.
    ts: float | None = None


def _read_slot() -> Reading:
    """Read the writer slot once and describe it. Never raises, never guesses.

    ``_storage_download`` answers with the empty bytes for the 404 that means
    *there is no such object* and with ``None`` when the call itself failed — a
    distinction this module already keeps for the backup — so *nobody has claimed
    the bucket* and *the bucket did not answer* do not become one answer.

    A record nobody can parse is :data:`Reading.UNKNOWN` rather than a claim left
    behind, and that is load-bearing: reading it as stale would invite a takeover
    of a bucket whose real owner is alive, which is the loss the lease exists to
    stop. Reading it as *nobody* would be worse — a second copy would go
    unannounced.
    """
    if not _cloud_has_data:
        return Reading(Reading.UNKNOWN)
    raw = _storage_download(LEASE_FILE, quiet=True)
    if raw is None:
        return Reading(Reading.UNKNOWN)
    if not raw:
        return Reading(Reading.FREE)
    try:
        lease = _json.loads(raw)
        owner = str(lease.get("owner") or "")
        ts = float(lease.get("ts") or 0)
    except Exception:
        return Reading(Reading.UNKNOWN)
    if not owner:
        return Reading(Reading.UNKNOWN)
    if owner == _INSTANCE_ID:
        return Reading(Reading.FREE, owner, ts)
    age = time.time() - ts
    if age > LEASE_TTL:
        print(
            f"[CLOUD SYNC] Taking over the sync lease from '{owner}' "
            f"(idle for {age / 60:.0f} min).",
            flush=True,
        )
        return Reading(Reading.FREE, owner, ts)
    return Reading(Reading.LIVE, owner, ts)


def _lease_problem() -> str | None:
    """Whether some other live instance owns the bucket.

    The backup's half of the question :func:`_read_slot` answers — a claim this
    machine may not write past. The boot's half is :func:`other_copy_verdict`.
    """
    reading = _read_slot()
    if reading.state != Reading.LIVE or reading.holder is None or reading.ts is None:
        return None
    age = max(0.0, time.time() - reading.ts)
    return (
        f"'{reading.holder}' owns the bucket ({age / 60:.1f} min ago) — running two "
        "instances of the bot against one backup is how progress gets reverted"
    )


def _service_of(instance: str) -> str | None:
    """The hosting *service* an instance name belongs to, when it names one.

    Render names a container ``srv-daiqibp594qs739qebc0-hibernate-7999965fd6-qwrf4``:
    the service id, the service's own name, the deployment hash, then the pod. Two
    containers of one deployment share the ``srv-<id>`` head and differ after it.

    Hugging Face names a container after its Space as well —
    ``r-1tsrajowo-meow-zxho7yru-d2319-tph82`` is the ``r-`` prefix, then the owner
    and the space, then a build hash, a replica hash and the pod — and it
    deliberately gets **no rule here**: there is nothing between the owner and
    the space name, and nothing in the name says where the space name ends, so
    ``meow`` and ``meow-dev`` are two Spaces whose containers read as one
    deployment. Suppressing the warning for the second of those is suppressing it
    in the case it exists for — one token answering from two places — so a Space's
    own rebuilds are answered by :func:`other_copy_verdict` instead, which asks
    whether the claim is still *moving* rather than what it is called.
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


def _no_second_copy(reading: Reading) -> str:
    """The line for the two answers that are not a second copy.

    One sentence each, because the two are different facts and only one of them is
    a fact at all: one says the check looked and nobody is answering, the other
    says there is no reading to reason from. A check whose negative answers were
    silent cannot be told apart from one that never ran — which is why these lines
    exist — and a check that uses the same words for both is halfway back to that.
    """
    if reading.state == Reading.UNKNOWN:
        return (
            "[INSTANCE] This boot has no reading of the backup bucket — nothing "
            "was asked of it, or what it holds could not be read — so a second "
            "copy cannot be ruled out: commands fail at random while one answers "
            "beside this machine."
        )
    return (
        "[INSTANCE] No other copy of this bot is answering Discord — nothing to "
        "report."
    )


def other_copy_verdict(*, sleep=time.sleep) -> str | None:
    """A second copy of the bot's name, or ``None`` — and the line either way.

    A name in the bucket is not the same thing as a copy that is answering, and
    at a boot the difference is the whole question: a copy that has been replaced
    leaves its claim behind for up to :data:`LEASE_TTL`, so a machine that has
    just taken a deploy can read five minutes of "I am here" written by nobody.
    That is not hypothetical — an upload to the Hugging Face Space replaced its
    container, and the one that came up reported the pod it had left behind (dead
    for seven minutes by then) as another copy of the bot, in a card telling the
    operator to stop the copy they had just deployed. Two copies of one token is
    a fault that has to be announced; announcing the deployment somebody just
    made is how the warning stops being read.

    So the question is asked twice. A live copy renews its claim on its backup
    tick whether or not anything changed, so a claim that is *still there and has
    moved* is a copy that is talking, and one that is still there with the same
    timestamp is a claim nobody is holding. The watch outlasts that tick —
    :data:`COPY_WATCH_SECONDS` is derived from it — and it is a **blocking** call:
    the boot starts it in a thread and does not wait for it.

    **Every answer is said out loud**: the card is only sent when a copy is found,
    so the line is what the log keeps — see :func:`_no_second_copy` for the two
    lines a boot can print instead.

    ``sleep`` is injectable for the same reason the rest of this module's network
    is: what the rule *is* has to be testable without standing still for it.
    """
    before = _read_slot()
    if before.state != Reading.LIVE:
        print(_no_second_copy(before), flush=True)
        return None
    owner = before.holder or ""
    if _previous_container_of_this_service(owner):
        # Logged, never announced: this is one deployment, not two copies — see
        # :func:`_previous_container_of_this_service`.
        print(
            f"[INSTANCE] '{owner}' is this deployment's previous container, still "
            "holding the sync lease — not a second copy of the bot.",
            flush=True,
        )
        return None
    print(
        f"[INSTANCE] '{owner}' holds the sync lease — watching "
        f"{COPY_WATCH_SECONDS}s to see whether that claim is renewed before this "
        "boot believes it is a second copy...",
        flush=True,
    )
    sleep(COPY_WATCH_SECONDS)
    after = _read_slot()
    if (
        after.state == Reading.LIVE
        and before.ts is not None
        and after.ts is not None
        and after.ts > before.ts
    ):
        print(
            f"[INSTANCE] ⚠️ another copy of this bot is running "
            f"({after.holder or owner}) — commands will fail at random until one "
            "of them stops.",
            flush=True,
        )
        return after.holder or owner
    print(_no_second_copy(after), flush=True)
    return None


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
    global _last_sync_hash, _last_snapshot_ts, _last_upload_ts, _consecutive_upload_failures
    global _last_refusal_reason

    reason = upload_refusal_reason()
    # Remembered, not merely logged: this is the sentence a reader needs when
    # the uploads have quietly stopped, and a host log is where it used to live.
    _last_refusal_reason = reason
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
        _consecutive_upload_failures += 1
        if _consecutive_upload_failures >= 3:
            print(
                f"[CLOUD SYNC] 🚨 ALERT: {_consecutive_upload_failures} consecutive backup uploads have failed! "
                "Database state may not be persisted.",
                flush=True,
            )
        return False

    _consecutive_upload_failures = 0

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
            await asyncio.sleep(SYNC_INTERVAL)
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
