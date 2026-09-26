"""Who reads a voice channel's chat without being in the voice channel.

A voice channel's text chat is the odd corner of Discord: it looks like a room
you are in only if you are in the voice channel, and every message in it is
readable by anybody who can see the channel — including people who never join
the call. So somebody sitting outside the call can read what is said inside it,
and nothing anywhere tells the people talking. That is what this module is for:
the person who wrote in the watched channel *without* being connected to it is
reported, once, to the channel the operator reads.

Two conditions are about the room rather than the person, and both are here
rather than in the listener because they are part of the same decision: the
watched channel has to really be a voice room (a *text* channel configured into
this setting would report every message in it, since nobody is ever "in" a text
channel's voice), and the message has to have been written by a person (a
webhook posts into that chat too, and a webhook is never in the call).

**What a bot can and cannot see, because the difference is the whole feature.**
Discord does not tell anybody that a channel was *opened* — not the bot, not the
guild's audit log, not a gateway event. So "somebody opened the voice chat" is
not a thing this can watch for, and a feature that claimed to would be a lie with
a clean-looking implementation. What is visible is a message arriving, which is
the act that matters anyway: reading without writing leaves no trace to find, but
speaking in a room you are not in does. That is the trigger here.

A member is considered "in" the channel when Discord says their voice state is in
it — ``member.voice`` — and **deafened or muted counts as being in it**, because
they are in the call and can hear the room. A member connected to a *different*
voice channel is not in this one, which is the case this is most often about.

The two addresses are settings rather than constants (``VC_WATCH_CHANNEL_ID`` and
``VC_WATCH_ALERT_CHANNEL_ID``): which channel is watched is a deployment fact, and
the compiled pair is the one this bot's own server uses. Either address left
blank switches the watch off, which is what an unset variable means everywhere
else in this project.

Nothing here talks to Discord beyond the objects it is handed, so the whole
decision is exercised with stand-ins in ``tests/test_vc_watch.py``.
"""
from __future__ import annotations

import discord

from utils import config_audit

#: The voice channel whose text chat is watched, and where a report goes. Both
#: are overridable, and both defaults are this bot's own server: the watch was
#: asked for there, and a server nobody configured gets no watch rather than a
#: channel id that means nothing in it.
_DEFAULT_WATCHED_CHANNEL_ID = 1546716356446130228
_DEFAULT_ALERT_CHANNEL_ID = 1553173598158454845

#: The channel kinds that are a voice room: a stage is one too, and its chat is
#: exactly as readable from outside as a voice channel's. The kind is read off
#: ``channel.type`` — the real discriminator, and one every stand-in in this
#: project's tests sets the same way the library does.
_VOICE_ROOMS = (discord.ChannelType.voice, discord.ChannelType.stage_voice)


#: The two settings, held as names rather than spelled at the point of reading.
#: Both reads go through ``_configured_id``, and the audit's source walk — the one
#: that fails when a declared setting has no reader — looks for the *name* of a
#: setting being read, which is exactly how every other name-holding constant in
#: this tree is spelled.
WATCHED_ENV = "VC_WATCH_CHANNEL_ID"
ALERT_ENV = "VC_WATCH_ALERT_CHANNEL_ID"


def _configured_id(setting: str, default: int) -> int:
    """One channel id from the environment, falling back on anything unusable.

    A malformed value falls back rather than raising, for the reason
    ``utils/bot_channels`` gives about its own: this is read on a message path,
    and a typo in a variable must not be the reason a listener errors on every
    message in the server.
    """
    raw = config_audit.get(setting)
    if not raw:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def watched_channel_id() -> int:
    """The voice channel whose chat is watched. ``0`` switches the watch off."""
    return _configured_id(WATCHED_ENV, _DEFAULT_WATCHED_CHANNEL_ID)


def alert_channel_id() -> int:
    """Where a report is posted. ``0`` switches the watch off."""
    return _configured_id(ALERT_ENV, _DEFAULT_ALERT_CHANNEL_ID)


def watching() -> bool:
    """Whether the watch is on: both addresses known, and not the same channel.

    The same channel in both fields is treated as off on purpose. It is what a
    half-filled configuration looks like, and the alternative — posting the alert
    back into the chat being watched — both leaks the watcher and hands whoever
    is reading the room a list of exactly who noticed.
    """
    watched, alert = watched_channel_id(), alert_channel_id()
    return bool(watched and alert and watched != alert)


def voice_channel_id(member) -> int | None:
    """The voice channel discord says this member is connected to, if any.

    ``member.voice`` is the gateway's own answer and it survives mute, deafen and
    going on camera — all of which are states of a person who is *in* the call.
    """
    voice = getattr(member, "voice", None)
    channel = getattr(voice, "channel", None)
    return int(channel.id) if channel is not None else None


def is_peek(message, *, watched: int | None = None) -> bool:
    """Whether this message is somebody reading the room they are not in.

    Deliberately pure and deliberately strict — every condition below is one
    that, dropped, makes the alert either useless or constant:

    * the watch is configured at all, including the same-channel rule in
      ``watching()``: this is the whole decision, so a caller does not have to
      know to ask ``watching()`` first and get it the wrong way round;
    * the message is in the watched channel, and that channel is a voice room
      rather than a text channel somebody configured by mistake;
    * it was written by a person: not a bot, and not a webhook. The bot's own
      posts and every other bot's land in the same channel, and a watch that
      reported itself would report on every message it is meant to be watching;
    * the author has a voice state to ask about — a DM has none, and reading a
      DM is not reading a room;
    * and their voice state is not this channel.
    """
    if watched is None:
        if not watching():
            return False
        watched = watched_channel_id()
    watched = int(watched)
    if not watched:
        return False
    channel = getattr(message, "channel", None)
    if channel is None or int(getattr(channel, "id", 0) or 0) != watched:
        return False
    if getattr(channel, "type", None) not in _VOICE_ROOMS:
        return False
    if getattr(message, "webhook_id", None):
        return False
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return False
    # A `User` — a DM's author, or anything else that is not in this guild — has
    # no voice state at all: `voice` only exists on a `Member`, and
    # `getattr(..., "voice", None)` is then None. Nobody to ask means nobody in
    # the call means nothing is being read.
    if getattr(author, "voice", None) is None and not hasattr(author, "guild"):
        return False
    return voice_channel_id(author) != watched


def facts(message) -> dict:
    """What the report says, gathered from the message rather than guessed.

    Returned as data so the card's author is the cog alone: this module decides
    *whether* something happened, and the surface decides how it reads.
    """
    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)
    content = str(getattr(message, "content", "") or "")
    here = voice_channel_id(author)
    return {
        "user_id": int(getattr(author, "id", 0) or 0),
        "name": str(
            getattr(author, "display_name", None) or getattr(author, "name", "somebody")
        ),
        "mention": str(getattr(author, "mention", "") or ""),
        "where": str(getattr(channel, "name", "the voice chat") or "the voice chat"),
        # Empty is a real answer and a common one: an attachment, a sticker or a
        # reply with nothing typed. The card says so instead of printing nothing.
        "text": content.strip()[:600],
        "attachments": len(getattr(message, "attachments", None) or []),
        # What they *are* connected to, when it is not this channel, so the
        # report can tell "in another call" from "nowhere in voice at all".
        "elsewhere": here,
        "jump": str(getattr(message, "jump_url", "") or ""),
    }


def resolve(channel_id: int, guild=None, find=None):
    """One address as a channel, looked up the way its *scope* requires.

    ``find`` is the deployment-wide lookup (a client's own ``get_channel``, which
    answers for every server the bot is in) and it is tried first, because the two
    addresses are deployment settings: the server an operator has in hand is very
    often **not** the server they name, and resolving against it made a watch that
    was working perfectly read as *nothing the bot can see* on every server but
    one. ``guild`` is the fallback for a caller that has only one server to look
    in.
    """
    if not channel_id:
        return None
    if callable(find):
        try:
            found = find(int(channel_id))
        except Exception:  # noqa: BLE001 — a lookup that cannot answer is not a verdict
            found = None
        if found is not None:
            return found
    lookup = getattr(guild, "get_channel", None) if guild is not None else None
    return lookup(int(channel_id)) if callable(lookup) else None


def address(channel_id: int, guild=None, find=None) -> dict:
    """One of the two addresses, as a surface can print it.

    ``resolved`` is the difference between "this id is a channel this bot can see"
    and "this id is set and nothing answers to it" — the second is the case that
    quietly does nothing (a typo, or a channel that went with a server the bot
    left), so it is a flag rather than a sentence somebody has to parse out of a
    name.
    """
    row = {
        "id": int(channel_id or 0),
        "name": f"`{channel_id}`",
        "mention": f"`{channel_id}`",
        "people": None,
        "resolved": False,
    }
    if not row["id"]:
        return row
    channel = resolve(row["id"], guild, find)
    if channel is None:
        return row
    row["resolved"] = True
    row["name"] = str(getattr(channel, "name", "") or row["name"])
    row["mention"] = str(getattr(channel, "mention", "") or row["mention"])
    members = getattr(channel, "members", None)
    if members is not None:
        try:
            row["people"] = len(list(members))
        except TypeError:
            row["people"] = None
    return row


def status(guild=None, find=None) -> dict:
    """The watch as an operator reads it: on or off, where, and what it can see.

    Two addresses are one *deployment* fact rather than a per-server one, so this
    answers for the process: ``find`` is the deployment-wide channel lookup and
    ``guild`` is only the last resort for a caller with one server. What the
    permission half checks is the one thing that can make the alert useless on a
    deployment that looks correctly configured — a bot that cannot post in the
    channel the report is meant for.

    ``why`` is the sentence for an *off* watch, because "off" on its own is the
    least useful thing a console can say about a feature somebody believes they
    turned on: an address set to zero and the same channel in both fields are
    both off, and they are fixed in different places.
    """
    watched, alert = watched_channel_id(), alert_channel_id()
    why = ""
    if not (watched and alert):
        why = (
            "One of the two addresses is 0, and 0 is this watch's own off switch — "
            "a blank variable is not, it falls back on the compiled pair."
        )
    elif watched == alert:
        why = (
            "Both addresses name the same channel, which is treated as off on "
            "purpose: a report posted into the room it is about would tell whoever "
            "is reading that room exactly who noticed them."
        )

    watched_row = address(watched, guild, find)
    alert_row = address(alert, guild, find)
    alert_channel = resolve(alert, guild, find)
    # The bot's own member *in the alert channel's server*, which is the server
    # whose permissions Discord will apply — not the one the console happens to be
    # reading. A channel's guild is where its permission reading lives.
    me = None
    if alert_channel is not None:
        channel_guild = getattr(alert_channel, "guild", None)
        me = getattr(channel_guild, "me", None) if channel_guild is not None else None
    if me is None and guild is not None:
        me = getattr(guild, "me", None)
    postable = None
    if me is not None and alert_channel is not None:
        try:
            perms = alert_channel.permissions_for(me)
        except Exception:  # noqa: BLE001 — a channel that cannot answer is not a finding
            perms = None
        if perms is not None:
            answered = [
                getattr(perms, name, None) for name in ("send_messages", "embed_links")
            ]
            if all(value is not None for value in answered):
                postable = all(bool(value) for value in answered)

    return {
        "on": watching(),
        "why": why,
        "watched": watched_row,
        "alert": alert_row,
        "postable": postable,
        # Whether *anything* could resolve the two ids. Without this the two rows
        # are settings, and "an id nothing answers to" and "an id nobody looked up"
        # are different sentences on the card — one is a watch that cannot fire.
        "resolvable": bool(callable(find) or callable(getattr(guild, "get_channel", None))),
    }


def elsewhere_name(message) -> str:
    """The name of the voice channel the author is in, or an honest "nowhere"."""
    author = getattr(message, "author", None)
    voice = getattr(author, "voice", None)
    channel = getattr(voice, "channel", None) if voice is not None else None
    if channel is None:
        return "not in any voice channel"
    return f"in **{getattr(channel, 'name', 'another voice channel')}**"
