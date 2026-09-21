"""Where a server's moderation record lives.

Three things now write to it — a moderator's own action, an *automatic* action
the bot took on its own, and an appeal from the member it happened to — and they
have to write to the same place, or the record is three lists that each look
complete on its own. :func:`post` is that place, and it is deliberately the only
one: the channel is resolved from ``log_channel_id``, validated as a real channel
in a real guild, and posted to through the normal card path.

**A refusal is a fact, not an exception.** A server that never ran ``/modlog``
has nowhere for a case to go, and a log channel somebody deleted leaves a stored
id pointing at nothing. Neither may take a command or a listener down, and
neither may pass silently either: both print one line naming the guild and what
was lost, because the alternative is a bot whose moderation leaves no trace and
says nothing about it. Discord's own audit log is not a substitute — it does not
survive channel deletion, it holds none of the *rule* that fired, and a member
appealing needs a thing a human can read.

The failure line is a plain ``print`` like the rest of the cog's diagnostics, so
it lands in the same host log the deployment already reads.
"""

from __future__ import annotations

import discord

from utils import db
from utils.style import send_card

#: The stored setting, shared with ``/modlog`` and the dashboard's log picker.
SETTING = "log_channel_id"


def _as_channel_id(raw) -> int | None:
    """``log_channel_id`` as an int, or ``None`` when it is not one.

    The value round-trips through JSON in ``guild_settings`` and has been written
    by hand-edited databases and one old ``/updatedb``: a non-numeric row is a
    thing that happened, not a thing that cannot.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def channel_for(guild: discord.Guild):
    """The channel this server logs moderation to, or ``None``.

    ``None`` covers the two cases a caller cannot act on differently — nothing
    was ever configured, and the configured channel is gone — because what a
    caller does about either is the same: say so, and do not pretend the record
    was written.
    """
    raw = await db.get_setting(guild.id, SETTING)
    channel_id = _as_channel_id(raw)
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        print(
            f"[MODLOG] Guild {guild.id} has {SETTING}={raw!r} but that channel does "
            "not exist; the entry was not logged."
        )
    return channel


async def post(guild: discord.Guild, embed: discord.Embed, *, what: str = "case") -> bool:
    """Put one card in the log channel. True when it landed somewhere.

    ``what`` completes the failure line — ``"the 'Ban' case #12"`` — because a
    host log that says ``a case was lost`` is a log somebody has to come back and
    ask questions about.
    """
    channel = await channel_for(guild)
    if channel is None:
        if await db.get_setting(guild.id, SETTING) is not None:
            print(f"[MODLOG] No usable log channel in guild {guild.id}; {what} has no record.")
        return False
    try:
        await send_card(channel, embed=embed)
        return True
    except discord.Forbidden:
        print(f"[MODLOG] No permission to post in {channel.mention} (guild {guild.id}); {what} has no record.")
        return False
    except discord.HTTPException as exc:
        # A rejected payload (too long, a dead webhook) must not take down the
        # command that was only trying to leave a record of itself.
        print(f"[MODLOG] Discord refused the card for {what} in guild {guild.id}: {exc}")
        return False
