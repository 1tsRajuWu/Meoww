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

# The announcement channel used to be spelled out here as well as in bot.py and
# the (now removed) uploads cog — three copies of one id, which is how the
# sticky post and the announcement ended up disagreeing. Announcements read the
# channel bot.py names; this module owns no channel id of its own.


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


#: The record as it was written before a server could have its own: one row for
#: the whole process. Kept as the fallback scope and as the migration source.
LEGACY_HASH_KEY = "changelog_hash"


def _hash_key(scope=None) -> str:
    """Where a scope's announced-state lives.

    One row for the whole process was wrong the moment the bot had a second
    server to announce to. A server whose update channel was unreachable either
    re-announced at every boot, or — if the write had not been guarded — consumed
    the note for the other server entirely, which is how an update goes missing
    on the server that could actually read it.
    """
    return LEGACY_HASH_KEY if scope in (None, "global") else f"changelog_hash:{int(scope)}"


def _read(db_conn, key: str) -> str:
    try:
        row = db_conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _write(db_conn, key: str, value: str) -> bool:
    """Store a row, reporting whether it landed.

    The migration below removes the old row once it has been handed on, and
deleting a record whose copy never landed is how the server that has been
reading these notes all along suddenly re-announces its whole changelog.
    """
    try:
        db_conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, value),
        )
        db_conn.commit()
        return True
    except Exception:
        return False


def _delete(db_conn, key: str) -> None:
    try:
        db_conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))
        db_conn.commit()
    except Exception:
        pass


def get_last_hash(db_conn, scope=None) -> str:
    """The hash ``scope`` was last announced — ``None`` for the legacy row."""
    return _read(db_conn, _hash_key(scope))


def set_last_hash(db_conn, hash_value: str, scope=None) -> None:
    """Record that ``scope`` has been told about ``hash_value``."""
    _write(db_conn, _hash_key(scope), hash_value)


def migrate_to_per_guild(db_conn, home_guild_id: int) -> None:
    """Hand the process-wide record to the server it actually describes.

    Without this, the server that has been receiving these notes all along would
    find its own (empty) row after the change and re-announce its entire
    changelog on the next boot — the loudest possible way to fix a quiet bug.
    Handing it on is one write, and only the server that owned the old channel
    may inherit it, so a newly added server still gets its first digest.
    """
    if not home_guild_id:
        return
    legacy = _read(db_conn, LEGACY_HASH_KEY)
    if not legacy:
        return
    if _read(db_conn, _hash_key(home_guild_id)):
        return
    if _write(db_conn, _hash_key(home_guild_id), legacy):
        # Consumed. Leaving the row in place would let *every* server that boots
        # next inherit it and never receive its own first note.
        _delete(db_conn, LEGACY_HASH_KEY)


#: Keep the announcement comfortably inside Discord's 4,096-character
#: embed description, leaving room for the intro line.
SUMMARY_BUDGET: int = 3300

#: How many changelog entries the digest names before it stops.
MAX_ITEMS: int = 6

#: How long a single entry is allowed to be in the digest.
ITEM_LIMIT: int = 200

_EMOJI = {
    "Added": "🆕",
    "Fixed": "🔧",
    "Changed": "⚙️",
    "Removed": "🗑️",
    "Security": "🔐",
    "Breaking": "⚠️",
    "Unreleased": "📝",
}


def _plain(text: str) -> str:
    """Strip the markdown that means nothing once it is out of the file."""
    import re

    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*`]", "", text)
    # Underscores only mean emphasis when they wrap a word. Stripping every one
    # of them turned SUPABASE_SERVICE_KEY into SUPABASESERVICEKEY in the note.
    text = re.sub(r"(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])", r"\1", text)
    return " ".join(text.split())


def _first_sentence(text: str) -> str:
    """The first sentence of a blob of prose."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[0] if parts else text.strip()


def _clip(text: str, limit: int = ITEM_LIMIT) -> str:
    """Trim to ``limit`` characters on a word boundary, with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


def _headline(bullet: str) -> str:
    """One readable line for a changelog bullet.

    The entries in this changelog lead with a bold claim (``**name**: then
    the detail``), so that claim plus one sentence is the whole summary. A
    bullet without one falls back to its first sentence.
    """
    import re

    match = re.search(r"\*\*(.+?)\*\*", bullet, flags=re.DOTALL)
    if match:
        lead = _plain(match.group(1)).rstrip(":")
        rest = _plain(bullet[match.end():]).lstrip(":—- 	")
        sentence = _first_sentence(rest) if rest else ""
        text = f"**{lead}**"
        if sentence:
            text += f" — {sentence}"
        return _clip(text)

    return _clip(_first_sentence(_plain(bullet)))


def summarize_changelog(
    full_text: str,
    max_items: int = MAX_ITEMS,
    budget: int = SUMMARY_BUDGET,
) -> str:
    """A compact, Discord-ready digest of the newest changelog section.

    The file is written newest-first, so the first section that has entries is
    the news. Only the headline of each entry is kept, and the whole digest is
    held inside ``budget`` characters — a longer one would be rejected by
    Discord outright, which is how an update announcement goes missing.
    """
    import re

    if not full_text:
        return "No changelog entries found."

    section_name: str | None = None
    bullets: list[str] = []
    for line in full_text.split("\n"):
        stripped = line.strip()
        heading = re.match(r"^##\s+(.+)$", stripped)
        if heading:
            if bullets:  # already collected a section with content
                break
            section_name = heading.group(1).strip()
            continue
        if section_name is None:
            continue
        if stripped.startswith(("- ", "* ")):
            entry = stripped[2:].strip()
            if len(_plain(entry)) > 3:
                bullets.append(entry)

    if not bullets:
        return _clip(_plain(full_text)[:800], 800)

    emoji = _EMOJI.get(section_name or "", "📝")
    header = f"**{emoji} {section_name or 'Changes'}**"

    lines: list[str] = []
    used = 0
    for entry in bullets[:max_items]:
        line = f"• {_headline(entry)}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1

    if not lines:  # a single enormous entry still has to say something
        lines = [f"• {_clip(_plain(bullets[0]), budget - 2)}"]

    remaining = len(bullets) - len(lines)
    digest = header + "\n" + "\n".join(lines)
    if remaining > 0:
        digest += f"\n-# …and {remaining} more in CHANGELOG.md"
    return digest
