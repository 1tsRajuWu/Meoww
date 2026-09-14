"""Version tracking for automatic update announcements.

Stores the last-announced changelog hash in the database so the bot
knows when there are new changes to post. On startup, if the
CHANGELOG.md hash differs from what was last announced, the new
entries are posted to the upload channel.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

#: The upload channel where update announcements go.
UPLOAD_CHANNEL_ID: int = 1548959110467289169


def _changelog_hash() -> str:
    """SHA-256 of the changelog file contents."""
    if not CHANGELOG.exists():
        return ""
    return hashlib.sha256(CHANGELOG.read_bytes()).hexdigest()[:16]


def _changelog_since_last(last_hash: str) -> str:
    """Extract changelog entries since the last announced hash.

    Returns the full changelog text if the hash doesn't match,
    or empty string if nothing new.
    """
    current_hash = _changelog_hash()
    if current_hash == last_hash:
        return ""
    if not CHANGELOG.exists():
        return ""
    return CHANGELOG.read_text(errors="replace").strip()


def get_last_hash(db_conn) -> str:
    """Read the last announced changelog hash from the database."""
    try:
        row = db_conn.execute(
            "SELECT value FROM kv_store WHERE key = 'changelog_hash'"
        ).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def set_last_hash(db_conn, hash_value: str) -> None:
    """Store the announced changelog hash in the database."""
    try:
        db_conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            ("changelog_hash", hash_value),
        )
        db_conn.commit()
    except Exception:
        pass
