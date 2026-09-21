"""The clickwrap: what a person agreed to, and which revision of it.

The Terms in this repository used to be **browsewrap** — linked at the bottom of
a page, agreed to by continuing to use the thing — and the legal framework this
module exists for is unambiguous about why that is not enough: terms a person was
never made to acknowledge are the terms most likely to be found unenforceable,
and the population that *cannot* acknowledge a hidden link is exactly the
population automated collection is made of. A crawler cannot assent, so a
prohibition it never saw was never a term.

So the dashboard asks. Three documents, one affirmative click, and a record of
it — because the three things a clickwrap must be able to answer afterwards are:

* **Which text?** Not "the Terms, at some point" — a revision. It is the SHA-256
  of the document files themselves, so an edit to a sentence changes the
  revision, and an acceptance from before that edit is visibly an acceptance of
  something else. The version line in the documents is for people; this is the
  one the software can check.
* **Who, and when, from where?** Account or scope, address, browser and time —
  the four facts that turn "they agreed" into something a disagreement can be
  settled with. The address and the browser are recorded because that is what
  they are for; nothing else about the request is kept.
* **Is it still in force?** The revision lives in the session, so a document that
  changes re-asks at the next page load rather than at the next sign-in. Sign-in
  alone would have meant a person who has been signed in since June accepts a
  September clause by continuing to click around.

Three rules the code keeps:

1. **Declining is a real door, and the same size as accepting.** Saying no ends
   the sign-in and explains what that means. It is not a hidden link, not a
   smaller button, not worded as a mistake, and it is never pre-selected —
   silence is not consent, so nothing is changed and nothing is continued by it.
2. **An unreadable ledger does not lock anybody out.** If the acceptances cannot
   be written, the click still works and the session still carries the revision;
   the page says the record could not be written. A person who has just agreed
   to the Terms must not be refused entry by the act of recording it.
3. **The gate covers the signed-in surfaces only.** It is asked of a session, not
   of a visitor: the public pages, the policies themselves and ``/health`` are
   readable without agreeing to anything, because a policy you must accept before
   you may read is not a policy.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time

from utils import db

#: The documents a sign-in agrees to, in the order they are shown. The Terms are
#: first because the anti-AI clause lives there; the AI policy is here at all
#: because it is the one a person can be *surprised* by — it says out loud that
#: training use is reserved, and a reservation nobody was shown is worth less.
DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("TERMS.md", "Terms of Service"),
    ("PRIVACY.md", "Privacy Policy"),
    ("AI_POLICY.md", "AI &amp; automated use"),
)

#: The operator-facing documents that are part of the same promise. A change to
#: the sub-processor list changes who receives data, so the revision moves with
#: it even though these two files are not shown on the page.
BUNDLE_EXTRAS = ("docs/legal/sub-processors.md",)

#: Where the revision lives in the session. Named for what it is rather than for
#: the day it was added: a stale key from an older deploy is ignored, not read.
SESSION_KEY = "legal_revision"

#: How many characters of the digest are shown and stored. Twelve hex characters
#: is 48 bits — plenty to tell two revisions of a policy apart, and short enough
#: that somebody can read it off a card and type it into a message.
VERSION_LENGTH = 12

#: The window ``/health`` reports. Same shape as every other count here: a
#: number, a date, and nothing that identifies anybody.
SUMMARY_CACHE_SECONDS = 60.0

TABLE = """CREATE TABLE IF NOT EXISTS legal_acceptances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL DEFAULT 0,
    version TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    guild_id INTEGER,
    user_id INTEGER,
    label TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT ''
)"""

INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_legal_acceptances_at "
    "ON legal_acceptances(at)"
)

_ensure_lock = threading.Lock()
#: Which database has been ensured, rather than *whether* one ever was. A process
#: that swaps databases under itself — a restore, a test pointing at a throwaway
#: file — would otherwise keep believing the table exists in the new one, and the
#: first insert would fail with "no such table" in a code path whose whole job is
#: to be the last thing that can fail.
_ensured_for: str | None = None
_version_lock = threading.Lock()
_version_cache: tuple[tuple[float, ...], str] | None = None
_summary: tuple[float, dict] | None = None
_write_failed = False


# --------------------------------------------------------------------------- #
# The revision
# --------------------------------------------------------------------------- #
def _document_paths() -> list[str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(root, name) for name, _ in DOCUMENTS] + [
        os.path.join(root, name) for name in BUNDLE_EXTRAS
    ]


def version() -> str:
    """The revision of the documents currently on disk.

    Content-addressed on purpose. A hand-maintained number is a number somebody
    forgets to move — this repository's own ``Version`` line was bumped by hand
    for four revisions, which is three chances for a clause to change while the
    number that says it changed stays where it was. The digest is over the bytes,
    so any edit at all is a new revision, including one that only adds a word.

    Cached against the files' mtimes, so the two reads per request behind the
    gate cost one ``stat`` each rather than a hash each.
    """
    global _version_cache
    paths = _document_paths()
    try:
        stamps = tuple(os.path.getmtime(path) for path in paths)
    except OSError:
        # A missing document is not a reason to refuse everybody: the revision
        # simply cannot be computed and the caller's answer is "ask again".
        return ""
    if _version_cache is not None and _version_cache[0] == stamps:
        return _version_cache[1]
    with _version_lock:
        if _version_cache is not None and _version_cache[0] == stamps:
            return _version_cache[1]
        digest = hashlib.sha256()
        for path in paths:
            try:
                with open(path, "rb") as handle:
                    digest.update(handle.read())
            except OSError:
                return ""
            digest.update(b"\x00")
        value = digest.hexdigest()[:VERSION_LENGTH]
        _version_cache = (stamps, value)
        return value


def document_label(name: str) -> str:
    """The human version line out of a document, if it has one."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            text = handle.read(2000)
    except OSError:
        return ""
    match = re.search(r"\*\*Version\s+(\d+)\*\*", text)
    return f"version {match.group(1)}" if match else ""


def needs(session) -> bool:
    """Whether this signed-in session has still to agree to the current text.

    ``False`` for a visitor: the gate is a property of being signed in, and a
    policy behind an acceptance wall is worse than one nobody reads.
    """
    if session.get("authenticated") is not True:
        return False
    live = version()
    if not live:
        # The documents could not be read. Asking somebody to agree to a
        # document this process cannot open is not a gate, it is a wall — so it
        # does not ask, and the console's Access page says the revision is
        # unknown.
        return False
    return session.get(SESSION_KEY) != live


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    global _ensured_for
    path = str(getattr(db, "DB_PATH", ""))
    if _ensured_for == path:
        return
    with _ensure_lock:
        if _ensured_for == path:
            return
        conn.execute(TABLE)
        conn.execute(INDEX)
        conn.commit()
        _ensured_for = path


def accept(*, scope: str = "", guild_id=None, user_id=None, label: str = "",
           ip: str = "", agent: str = "") -> bool:
    """Write down that somebody agreed, and answer whether it was written.

    A ``False`` is not a refusal: the caller still marks the session, because the
    person did agree and it is the *record* that failed. The page says so, in
    those words, rather than pretending the record exists.
    """
    global _write_failed
    revision = version()
    if not revision:
        return False
    try:
        conn = _connect()
        _ensure(conn)
        conn.execute(
            "INSERT INTO legal_acceptances "
            "(at, version, scope, guild_id, user_id, label, ip, agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                revision,
                scope or "",
                int(guild_id) if guild_id else None,
                int(user_id) if user_id else None,
                (label or "")[:120],
                (ip or "")[:64],
                (agent or "")[:300],
            ),
        )
        # A record, not a hoard: an acceptance older than the retention window
        # this project states for activity has served its purpose, and the
        # revision it names is still in the history of the documents.
        conn.execute(
            "DELETE FROM legal_acceptances WHERE at < ?",
            (time.time() - RETENTION_DAYS * 86400,),
        )
        conn.commit()
        conn.close()
        _write_failed = False
        return True
    except sqlite3.Error as exc:
        _write_failed = True
        print(f"[CONSENT] could not record the acceptance: {exc}", flush=True)
        return False


#: How long an acceptance row is kept. Longer than the activity window because it
#: is the one record that answers a question asked *after* an account is gone.
RETENTION_DAYS = 365 * 3


def reading(limit: int = 20) -> list[dict]:
    """The most recent acceptances, newest first — the operator's list."""
    try:
        conn = _connect()
        _ensure(conn)
        rows = conn.execute(
            "SELECT at, version, scope, guild_id, user_id, label, ip FROM legal_acceptances "
            "ORDER BY at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        item = dict(row)
        item["at"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(row["at"] or 0))
        item["ip"] = _mask(row["ip"] or "")
        out.append(item)
    return out


def _mask(value: str) -> str:
    """Half an address: enough to recognise, not enough to be a copy of it."""
    text = value or ""
    if "." in text:
        parts = text.split(".")
        return ".".join(parts[:2] + ["x"] * max(0, len(parts) - 2)) if len(parts) > 2 else text
    return text[:len(text) // 2] + "…" if len(text) > 8 else text


def totals(days: int = 30) -> dict:
    """Counts for ``/health``: how many, since when, and never who."""
    global _summary
    now = time.time()
    if _summary is not None and now - _summary[0] < SUMMARY_CACHE_SECONDS:
        return _summary[1]
    data = {"revision": version(), "accepted": 0, "last": None, "window_days": days}
    try:
        conn = _connect()
        _ensure(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(at) AS last FROM legal_acceptances WHERE at >= ?",
            (now - days * 86400,),
        ).fetchone()
        data["accepted"] = int(row["n"] or 0)
        if row["last"]:
            data["last"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["last"]))
        conn.close()
    except sqlite3.Error:
        data["ledger"] = "unreadable"
    if _write_failed:
        data["ledger"] = "unwritable"
    _summary = (now, data)
    return data


def writable() -> bool:
    """Whether the last attempt to write landed. The Access page says so."""
    return not _write_failed
