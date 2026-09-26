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

#: What a case card needs from the bot's own role *on the channel*, in Discord's
#: own words because a refusal quotes them back at whoever has to fix it. The
#: same pair ``utils.health`` names (``POSTING_NEEDS``), and separate from it on
#: purpose: that one is about the server's default role and reads Discord's
#: answer for the bot's *member*, while this one is a channel's own overwrites.
CARD_NEEDS = (
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
)


def can_post(channel, me) -> bool | None:
    """Whether a case card would land in ``channel``; ``None`` when Discord was not asked.

    A channel's answer, not the server's: ``send_messages`` is what Discord
    requires for the message to appear at all and ``embed_links`` for the card it
    is drawn as, and a server whose role has both can still have a channel that
    takes neither away.

    ``None`` is the third answer and it is deliberately not ``False``: a channel
    the bot cannot read permissions on, and a stand-in that has never heard of
    ``embed_links``, both produce it. Reporting either as "the bot cannot post
    here" is a finding invented by a missing reading — the defect ``utils/health``
    opens with.
    """
    if me is None:
        return None
    try:
        perms = channel.permissions_for(me)
    except Exception:  # noqa: BLE001 — a channel that cannot answer is not a refusal
        return None
    answered = [getattr(perms, name, None) for name, _words in CARD_NEEDS]
    if any(value is None for value in answered):
        return None
    return all(bool(value) for value in answered)


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
