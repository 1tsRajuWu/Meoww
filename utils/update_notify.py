"""Update notification system for the bot.

Sends formatted update messages to a dedicated channel, pinging a role
so members who care about changes can be notified. Every message is
structured with clear sections so readers can quickly see what changed,
what was fixed, and what APIs are behind each feature — without
exposing secrets.
"""
from __future__ import annotations

import discord

from utils import bot_channels, config_audit

# The channel where update messages are posted, and the role that gets pinged.
#
# Both defaults were written down here *and* in ``bot.py``, which is how a ping and
# the gate on the operator's side of ``/qna`` — the two things these two ids decide
# — could disagree about who is an operator and where the bot's own server is. They
# read the module that owns that question instead, so there is one copy of each.
UPDATE_CHANNEL_ID: int = int(
    config_audit.get("UPDATE_CHANNEL_ID", str(bot_channels.LEGACY_UPDATE_CHANNEL_ID))
)
UPDATE_ROLE_ID: int = int(
    config_audit.get("UPDATE_ROLE_ID", str(bot_channels.DEV_ROLE_ID))
)


def _fmt_section(title: str, lines: list[str]) -> str:
    """Format one section of an update message."""
    if not lines:
        return ""
    body = "\n".join(f"  • {line}" for line in lines)
    return f"**{title}**\n{body}"


def build_update_embed(
    *,
    version: str,
    title: str,
    fixed: list[str] | None = None,
    added: list[str] | None = None,
    improved: list[str] | None = None,
    data_sources: list[str] | None = None,
    notes: list[str] | None = None,
    color: int = 0x5865F2,
) -> discord.Embed:
    """Build a rich embed for an update notification.

    Parameters
    ----------
    version:
        Short version tag (e.g. ``"v2.14"`` or ``"2026-09-14"``).
    title:
        One-line summary of the update.
    fixed:
        Bugs that were resolved.
    added:
        New features or commands.
    improved:
        Enhancements to existing features.
    data_sources:
        Where live data comes from (APIs, exchanges). Never include keys.
    notes:
        Any additional context.
    color:
        Embed accent color.
    """
    embed = discord.Embed(
        title=f"🐾 Bot Update — {title}",
        color=color,
    )

    sections = []
    if added:
        sections.append(_fmt_section("🆕 Added", added))
    if fixed:
        sections.append(_fmt_section("🐛 Fixed", fixed))
    if improved:
        sections.append(_fmt_section("⚡ Improved", improved))
    if data_sources:
        sections.append(_fmt_section("📡 Data Sources", data_sources))
    if notes:
        sections.append(_fmt_section("📝 Notes", notes))

    embed.description = "\n\n".join(sections) if sections else "No details provided."
    embed.set_footer(text=f"Version {version}")
    return embed


async def send_update(
    bot: discord.Client,
    *,
    version: str,
    title: str,
    fixed: list[str] | None = None,
    added: list[str] | None = None,
    improved: list[str] | None = None,
    data_sources: list[str] | None = None,
    notes: list[str] | None = None,
    ping_role: bool = True,
    color: int = 0x5865F2,
) -> bool:
    """Send an update notification to the designated channel.

    Returns True if the message was sent successfully.
    """
    channel = bot.get_channel(UPDATE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(UPDATE_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            print(f"[UPDATE] ❌ Channel {UPDATE_CHANNEL_ID} not found or inaccessible.", flush=True)
            return False

    embed = build_update_embed(
        version=version,
        title=title,
        fixed=fixed,
        added=added,
        improved=improved,
        data_sources=data_sources,
        notes=notes,
        color=color,
    )

    content = f"<@&{UPDATE_ROLE_ID}>" if ping_role else None
    try:
        await channel.send(content=content, embed=embed)
        print(f"[UPDATE] ✅ Update notification sent to #{getattr(channel, 'name', 'unknown')}.", flush=True)
        return True
    except discord.Forbidden:
        print(f"[UPDATE] ❌ No permission to send in channel {UPDATE_CHANNEL_ID}.", flush=True)
        return False
    except Exception as exc:
        print(f"[UPDATE] ❌ Failed to send update: {exc}", flush=True)
        return False
