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

# The upload channel used to be spelled out here as well as in cogs.uploads
# and bot.py — three copies of one id, which is how the sticky post and
# /upload ended up disagreeing. Announcements go to the channel bot.py names;
# the upload channel lives in cogs.uploads.DEFAULT_UPLOAD_CHANNEL_ID.


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
    # of them turned UPLOAD_CHANNEL_ID into UPLOADCHANNELID in the update note.
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
