#!/usr/bin/env python3
"""Say sorry to the people a card never tagged — once, on purpose, by hand.

Cards suppress mentions by default (``utils.style.NO_PINGS``), so every roleplay
card of the form ``@you poked @them`` rendered both names and notified **nobody**.
That is fixed going forward. This script is for what already happened: it finds the
affected messages inside one window, and replies to each of them tagging the people
who should have been told, with a short note that names the mistake.

It is written to be safe to run on a live server, which is the only place it can do
anything useful:

* **Nothing is posted without ``--apply``.** The default run only reads history and
  prints what it *would* do, message by message.
* **It cannot double-post.** Every message it answers is recorded in
  ``guild_settings`` under :data:`MARKER_KEY`, and a message that is already in that
  list is skipped — including across a crash halfway through, because the marker is
  written per message as it goes.
* **It only ever replies.** No edits, no deletes, no DMs.

Usage (from the project root, with ``DISCORD_TOKEN`` in the environment or ``.env``)::

    # what would happen, for the small hours of the 21st, local time
    python scripts/repair_untagged_actions.py --guild Hmm --channel general \\
        --date 2026-09-21 --hours 1-5 --tz +05:30

    # ...and then, once that reads correctly
    python scripts/repair_untagged_actions.py --guild Hmm --channel general \\
        --date 2026-09-21 --hours 1-5 --tz +05:30 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # the documented local setup keeps the token in .env
    from dotenv import load_dotenv

    load_dotenv()
    _ENV_LOADED = True
except Exception:  # pragma: no cover - a host that exports its own environment
    _ENV_LOADED = False

import discord  # noqa: E402  (after the path fix, on purpose)

from utils import db  # noqa: E402
from utils.config_audit import get as env_get  # noqa: E402

#: Where the "already answered" list lives, so a second run is a no-op.
MARKER_KEY = "untagged_action_repair"

#: A raw mention, which is what a Components V2 message carries: the text lives in
#: components, so ``message.mentions`` can be empty even when people are named.
MENTION = re.compile(r"<@!?(\d+)>")

#: How many messages one run will read per channel. Enough for a quiet night on a
#: busy server; the window filter does the real work.
SCAN_LIMIT = 500


def parse_hours(spec: str) -> tuple[float, float]:
    """``"1-5"`` -> ``(1.0, 5.0)``, as hours of the day."""
    text = str(spec or "").strip()
    if "-" not in text:
        raise argparse.ArgumentTypeError("hours must look like 1-5")
    start_text, _, end_text = text.partition("-")
    try:
        start, end = float(start_text), float(end_text)
    except ValueError:
        raise argparse.ArgumentTypeError("hours must be numbers, like 1-5") from None
    if not (0 <= start < end <= 24):
        raise argparse.ArgumentTypeError("hours must satisfy 0 <= start < end <= 24")
    return start, end


def parse_tz(spec: str) -> dt.timedelta:
    """``"+05:30"`` -> a fixed offset. Fixed on purpose: the window is stated by a
    human in their own clock, and a DST rule would move it under them."""
    text = str(spec or "").strip()
    match = re.fullmatch(r"([+-]?)(\d{1,2}):?(\d{2})", text)
    if not match:
        raise argparse.ArgumentTypeError("tz must look like +05:30")
    sign = -1 if match.group(1) == "-" else 1
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 14 or minutes > 59:
        raise argparse.ArgumentTypeError("tz must be within ±14:00")
    return sign * dt.timedelta(hours=hours, minutes=minutes)


def normalise_channel_name(name: str) -> str:
    """A channel name reduced to letters and digits.

    Real servers decorate: the channel this was written for is ``#💬・general``,
    so an exact-name lookup finds nothing and reads like the bot is not in the
    server. Comparing the bare words is what makes ``--channel general`` mean what
    a person means by it.
    """
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def match_channel(channels, wanted: str) -> list:
    """Every channel that could be what was asked for (exact first, then suffix)."""
    key = normalise_channel_name(wanted)
    if not key:
        return []
    channels = list(channels)
    exact = [channel for channel in channels if normalise_channel_name(getattr(channel, "name", "")) == key]
    if exact:
        return exact
    return [
        channel
        for channel in channels
        if normalise_channel_name(getattr(channel, "name", "")).endswith(key)
    ]


def window(date: dt.date, hours: tuple[float, float], tz: dt.timedelta) -> tuple[dt.datetime, dt.datetime]:
    """The half-open UTC window for a local date and hour range."""
    start_hour, end_hour = hours
    base = dt.datetime.combine(date, dt.time(), tzinfo=dt.timezone(tz))
    return base + dt.timedelta(hours=start_hour), base + dt.timedelta(hours=end_hour)


def in_window(when: dt.datetime, start: dt.datetime, end: dt.datetime) -> bool:
    """Whether a message falls inside the window (both ends UTC-aware)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return start <= when < end


def _component_text(components) -> str:
    """Every readable string inside a components tree — **dicts or objects**.

    Both shapes have to be walked, and that is not a guess: ``Message.components``
    on a received card is a list of parsed component *objects*
    (``Container`` › ``SectionComponent`` › ``TextDisplay``), while
    ``to_components()`` — on a view this code built itself — is the JSON dict form.
    Reading only one of the two is how the first version of this script found no
    cards at all in a channel full of them.
    """
    out: list[str] = []
    stack: list = [components]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("content", "url", "label", "placeholder") and isinstance(value, str):
                    out.append(value)
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        else:
            content = getattr(node, "content", None)
            if isinstance(content, str):
                out.append(content)
            label = getattr(node, "label", None)
            if isinstance(label, str):
                out.append(label)
            children = getattr(node, "children", None)
            if children:
                stack.extend(children)
            accessory = getattr(node, "accessory", None)
            if accessory is not None:
                stack.append(accessory)
    return "\n".join(out)


def message_text(message) -> str:
    """Everything readable in a message, V2 or legacy."""
    parts = [str(getattr(message, "content", "") or "")]
    for embed in getattr(message, "embeds", None) or []:
        parts.append(str(getattr(embed, "description", "") or ""))
        parts.append(str(getattr(embed, "title", "") or ""))
    components = getattr(message, "components", None)
    if components:
        try:
            serialized = components.to_components() if hasattr(components, "to_components") else components
        except Exception:
            serialized = components
        parts.append(_component_text(serialized))
    return "\n".join(part for part in parts if part)


def mentioned_ids(message, *, exclude: set[int] | None = None) -> list[int]:
    """The human ids a message names, in order, without duplicates.

    Reads both the API's own ``mentions`` list and the raw ``<@id>`` text, because
    a V2 message can carry the mention in a component while the parsed list is
    empty — which is exactly the shape of the cards this script exists for.
    """
    skip = set(exclude or ())
    seen: dict[int, None] = {}
    for user in getattr(message, "mentions", None) or []:
        user_id = int(getattr(user, "id", 0) or 0)
        if user_id and user_id not in skip:
            seen[user_id] = None
    for match in MENTION.finditer(message_text(message)):
        user_id = int(match.group(1))
        if user_id and user_id not in skip:
            seen[user_id] = None
    return list(seen)


def note_text(mentions: list[str], excerpt: str) -> str:
    """The apology itself, as one message.

    Kept short, and kept to what is true: the message existed, it named them, and
    it never notified them.
    """
    who = " ".join(mentions)
    line = " ".join(str(excerpt or "").split())
    if len(line) > 300:
        line = line[:299].rstrip() + "…"
    apology = (
        "This message named you but never actually tagged you, which is our fault, "
        "not yours. It's fixed now, so the next one will reach you."
    )
    if not line:
        return f"{who} — sorry for the bug! 🐾\n{apology}"
    return f"{who} — sorry for the bug! 🐾\n{apology}\n\n> {line}"


async def _already_replied(guild_id: int) -> set[int]:
    stored = await db.get_setting(int(guild_id), MARKER_KEY, None)
    if isinstance(stored, (list, tuple)):
        return {int(value) for value in stored if str(value).isdigit()}
    if isinstance(stored, str) and stored.strip():
        return {int(value) for value in re.findall(r"\d+", stored)}
    return set()


async def _remember(guild_id: int, answered: set[int]) -> None:
    await db.set_setting(int(guild_id), MARKER_KEY, sorted(answered))


def _looks_like_a_card(message, bot_id: int) -> bool:
    """A bot message in this channel that names at least one human.

    Not narrowed to the ten roleplay titles on purpose: the same suppression hid
    ``/rizz`` and ``/roast``, and a whitelist of titles is the kind of list that is
    wrong the day a card is added. The window and the marker are the safety, not a
    guess about wording.
    """
    if int(getattr(getattr(message, "author", None), "id", 0) or 0) != bot_id:
        return False
    return bool(mentioned_ids(message, exclude={bot_id}))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guild", required=True, help="server id, or its exact name")
    parser.add_argument("--channel", required=True, help="channel id, or its exact name")
    parser.add_argument("--date", required=True, help="local date the window belongs to, YYYY-MM-DD")
    parser.add_argument("--hours", type=parse_hours, default=(1.0, 5.0), help="local hour range, e.g. 1-5")
    parser.add_argument("--tz", type=parse_tz, default=dt.timedelta(hours=5, minutes=30), help="local offset, e.g. +05:30")
    parser.add_argument("--apply", action="store_true", help="actually post the replies (default: dry run)")
    parser.add_argument("--limit", type=int, default=SCAN_LIMIT, help="messages to read in the channel")
    args = parser.parse_args(argv)

    try:
        date = dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD, not {args.date!r}")
        return 2

    start, end = window(date, args.hours, args.tz)
    print(f"Window: {start.isoformat()} → {end.isoformat()}  (local {date} {args.hours[0]:g}-{args.hours[1]:g} {args.tz})")
    if not args.apply:
        print("Dry run: reading history only. Pass --apply to post the replies.")

    token = env_get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set (environment or .env) — nothing to connect with.")
        return 2

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    async def on_ready() -> None:
        try:
            await run(client)
        except Exception as exc:  # a script that reports beats a script that hangs
            print(f"Failed: {type(exc).__name__}: {exc}")
            raise
        finally:
            await client.close()

    async def run(bot: discord.Client) -> None:
        guild = bot.get_guild(int(args.guild)) if str(args.guild).isdigit() else discord.utils.get(bot.guilds, name=args.guild)
        if guild is None:
            print(f"No server matching {args.guild!r} — the bot must be in it.")
            print("Servers this bot can see:", ", ".join(f"{g.name} ({g.id})" for g in bot.guilds))
            return
        if str(args.channel).isdigit():
            channel = bot.get_channel(int(args.channel))
        else:
            matches = match_channel(guild.text_channels, args.channel)
            if len(matches) > 1:
                listing = ", ".join(f"#{c.name} ({c.id})" for c in matches)
                print(f"{args.channel!r} matches more than one channel — use an id: {listing}")
                return
            channel = matches[0] if matches else None
        if channel is None:
            names = " ".join(f"#{c.name}" for c in guild.text_channels[:40]) or "(none visible)"
            print(f"No channel matching {args.channel!r} in {guild.name} ({guild.id}).")
            print(f"Text channels here: {names}")
            return

        answered = await _already_replied(guild.id)
        print(f"Server: {guild.name} ({guild.id}) · channel: #{channel.name} · already answered: {len(answered)}")

        found = 0
        for message in await _history(channel, args.limit):
            if not in_window(message.created_at, start, end):
                continue
            if not _looks_like_a_card(message, bot.user.id):
                continue
            if message.id in answered:
                print(f"  · {message.id} already answered — skipped")
                continue

            ids = mentioned_ids(message, exclude={bot.user.id})
            names, mentions = [], []
            for user_id in ids:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        continue
                if member.bot and member.id == bot.user.id:
                    continue
                names.append(member.display_name)
                mentions.append(member.mention)
            if not mentions:
                print(f"  · {message.id}: names nobody who is still in the server — skipped")
                continue

            found += 1
            excerpt = message_text(message)
            body = note_text(mentions, excerpt)
            print(f"  → {message.created_at.isoformat()} {message.id}: {', '.join(names)}")
            # The excerpt is printed in full on purpose: this is the review pass,
            # and the only way to tell a card that deserved a tag from one that
            # merely happened to name somebody is to read what it said.
            print(f"      what it said: {' '.join(excerpt.split())[:220]}")
            if not args.apply:
                continue
            try:
                await message.reply(body, mention_author=False, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException as exc:
                print(f"    ! could not reply: {exc}")
                continue
            answered.add(message.id)
            await _remember(guild.id, answered)
            await asyncio.sleep(1.0)  # be a polite API citizen

        verb = "replied to" if args.apply else "would reply to"
        print(f"{verb} {found} message(s).")

    client.event(on_ready)
    await client.start(token)
    return 0


async def _history(channel, limit: int):
    """Messages in a channel, oldest-first, without requiring history permissions
    it does not have."""
    collected = []
    try:
        async for message in channel.history(limit=limit, oldest_first=False):
            collected.append(message)
    except discord.Forbidden:
        print(f"Missing 'Read Message History' in #{getattr(channel, 'name', channel)}.")
        return []
    return list(reversed(collected))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
