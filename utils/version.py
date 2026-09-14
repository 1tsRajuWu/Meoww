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


def summarize_changelog(full_text: str) -> str:
    """Extract and format the changelog entries into a Discord-ready summary.

    Parses ``## Heading`` sections and their bullet points, returns a formatted
    string grouped by section (Added, Fixed, Changed, etc.).
    """
    if not full_text:
        return "No changelog entries found."

    import re

    lines = full_text.split("\n")
    sections: dict[str, list[str]] = {}
    current_section = None

    for line in lines:
        # Match heading like "## Heading"
        heading_match = re.match(r"^##\s+(.+)$", line.strip())
        if heading_match:
            current_section = heading_match.group(1).strip()
            sections[current_section] = []
            continue

        if current_section is None:
            continue

        # Only include bullet points under headings
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            # Clean up the bullet — remove leading dash/star and whitespace
            content = stripped[2:].strip()
            # Cut off any trailing markdown links or code blocks for brevity
            content = re.sub(r"\s*\[.*\]\([^)]*\)", "", content)
            content = re.sub(r"`[^`]*`", "", content)
            if content and len(content) > 3:  # Skip very short fragments
                sections[current_section].append(content)

    # Build the summary
    if not sections:
        return "Changelog updated.\n\n" + full_text[:500]

    # Emoji mapping for common section names
    emoji_map = {
        "Added": "🆕",
        "Fixed": "🔧",
        "Changed": "⚙️",
        "Removed": "🗑️",
        "Security": "🔐",
        "Breaking": "⚠️",
        "Changelog": "📝",
        "Unreleased": "📝",
    }

    parts = []
    for section, items in sections.items():
        header = section.strip()
        emoji = emoji_map.get(header, "📝")
        # Skip "Unreleased" as a standalone section header if we have content sections
        if header in ("Unreleased", "Changelog") and len(sections) > 1:
            continue
        if not items:
            continue

        # Limit to first 8 items per section to keep embed readable
        display_items = items[:8]
        if len(items) > 8:
            display_items.append(f"...and {len(items) - 8} more")

        parts.append(f"**{emoji} {header}**\n" + "\n".join(f"• {item}" for item in display_items))

    summary = "\n\n".join(parts)

    # If there are more than 5 sections, mention it
    if len(sections) > 5:
        summary += f"\n\n…and {len(sections) - 5} more sections (view CHANGELOG.md for full details)"

    return summary
