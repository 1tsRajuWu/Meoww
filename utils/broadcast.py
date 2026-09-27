"""One message, sent to every server the bot is in — and the list of who gets it, first.

This is the only thing in the product that talks to **every** server at once, so
it is deliberately not a system any server can configure: it is an action inside
the operator's god level, beside the pause and the two fleet switches, and its
failure mode is not a wrong setting in one place but a sentence broadcast to every
member of every server the bot can reach. Three decisions make it safe to have.

**The message is a template, because one sentence cannot be right for two
servers.** ``{server}``, ``{members}``, ``{systems}`` and ``{bot}`` are substituted
per server — a token this module does not know is **refused**, not left in the
message: ``{server}`` arriving literally in four hundred servers is a mistake
nobody can take back, and the operator is the only person who can say what they
meant.

**Where it goes is each server's own answer.** :mod:`utils.bot_channels` owns
"where does this server want the bot to talk", and the announcement lands in the
channel a server bound for ``update``; a server that has bound none falls back to
Discord's own *system channel* for that guild, which is the one channel a server
has already told Discord is for service messages. A server with neither is
**named as unreached, with the reason** — never guessed at, because a broadcast
that quietly skipped somebody is worse than one that says who it skipped.

**Nothing is pinged unless the operator asked, and then only a role.**
``@everyone`` and ``@here`` are unreachable from here: the mention allowance is
built by hand rather than left to Discord's defaults, precisely because the
message is a template and a *server's own name* is interpolated into it — a guild
called ``@everyone`` must not be able to mention anybody, and the only way to be
sure of that is to state what may be mentioned.

**The preview is this same pass with the send removed**, so what the operator
reads before pressing the other button is what would happen rather than a second
implementation's idea of it. Both go through :func:`run`.
"""
from __future__ import annotations

import asyncio

from utils import bot_channels, brand, features, modules

#: The tokens a template may use, in the order the console offers them. Declared
#: here rather than written into the page, so the sentence that explains them and
#: the check that refuses an unknown one cannot disagree.
PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("server", "the server's own name"),
    ("members", "how many people are in it"),
    ("systems", "how many of the bot's systems it has switched on"),
    ("bot", "the bot's own display name"),
)

#: Long enough for a paragraph, short enough that it is still a notice rather than
#: documentation. Discord's own ceiling is 2000 characters and the mention prefix
#: is added on top, so this leaves room rather than sitting on the edge.
MAX_CHARS = 1200

#: Between two sends, and only between: Discord's limits are per channel and these
#: are all different channels, so this is politeness rather than arithmetic — a
#: fleet of a few dozen is a few dozen requests arriving at once otherwise.
SEND_SPACING_SECONDS = 0.4

#: Why a server could not be reached, in words, because the operator reads them.
NO_CHANNEL = "no channel is bound for bot news, and it has no system channel"
NO_PERMISSION = "the bot may not post in the channel it would use"


class Refused(ValueError):
    """The template cannot be sent as written, and nothing was."""


def unknown_placeholders(template: str) -> tuple[str, ...]:
    """Tokens in ``{braces}`` that this module does not substitute.

    Deliberately finds *any* brace pair rather than only the shapes that look like
    ours: ``{server`` or ``{ serve r}`` is a typo the operator meant as a token,
    and the answer to a typo is the sentence naming the four that work.
    """
    known = {name for name, _what in PLACEHOLDERS}
    found: list[str] = []
    for index, char in enumerate(template or ""):
        if char != "{":
            continue
        end = template.find("}", index + 1)
        if end == -1:
            continue
        token = template[index + 1:end].strip()
        if token and token not in known and token not in found:
            found.append(token)
    return tuple(found)


def validate(template: str) -> str:
    """The template as it will be sent, or :class:`Refused` with the reason.

    The only reader of the two rules, so the page, the API and the audit all
    answer the same way about the same text.
    """
    text = (template or "").strip()
    if not text:
        raise Refused("Write the message first — there is nothing to send.")
    unknown = unknown_placeholders(text)
    if unknown:
        known = ", ".join("{%s}" % name for name, _what in PLACEHOLDERS)
        raise Refused(
            f"{{{unknown[0]}}} is not a token this can fill in. The four it can are "
            f"{known} — anything else would arrive as the braces you typed."
        )
    if len(text) > MAX_CHARS:
        raise Refused(
            f"That message is {len(text)} characters and the limit is {MAX_CHARS}. "
            "A notice that has to be scrolled is one nobody reads."
        )
    return text


def systems_on(guild_id) -> int | None:
    """How many systems one server has switched on, or ``None`` if unreadable.

    Read through the same cache the dashboard reads, so the number in a broadcast
    is the number that server's own page shows. A read that fails answers
    ``None``, which the template renders as a dash: a count that could not be
    taken is not a zero.
    """
    try:
        states = modules.get_states(int(guild_id))
    except Exception:  # noqa: BLE001 — a placeholder is never worth a refusal
        return None
    count = 0
    for module in features.dashboard_modules():
        if states.get(module["id"], features.default_enabled(module["id"])):
            count += 1
    return count


def render(template: str, row: dict) -> str:
    """One server's copy of the message.

    ``row`` carries the four values by name. A value that could not be read is the
    em dash this project uses everywhere for "nobody has taken this reading" —
    the template keeps its sentence and the operator can see which figure is
    missing rather than reading a wrong number.
    """
    text = template
    for name, _what in PLACEHOLDERS:
        value = row.get(name)
        if value is None:
            value = "—"
        text = text.replace("{" + name + "}", str(value))
    return text


def _may_post(channel, me) -> bool:
    """Whether the bot may put a message in this channel, asked of Discord's rules."""
    if me is None:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    if permissions_for is None:
        return False
    try:
        permissions = permissions_for(me)
    except Exception:  # noqa: BLE001 — an unreadable permission is not a permission
        return False
    return bool(getattr(permissions, "send_messages", False))


def _target(guild, bound_id=None) -> tuple[object | None, str, str]:
    """Where this server's copy goes, how it got there, and why not.

    Returns ``(channel, via, reason)`` where ``via`` is ``"update"`` for a channel
    the server bound, ``"system"`` for Discord's own service channel, and ``""``
    with a reason when there is no target at all.

    ``bound_id`` is passed in because reading it is a database call: the caller is
    the async pass below, and a resolver that awaited one setting per guild inline
    would make this function async for a single read that happens before it.
    """
    me = getattr(guild, "me", None)
    if bound_id is not None:
        channel = guild.get_channel(bound_id)
        if channel is not None and _may_post(channel, me):
            return channel, "update", ""
    system = getattr(guild, "system_channel", None)
    if system is not None and _may_post(system, me):
        return system, "system", ""
    if bound_id is not None:
        channel = guild.get_channel(bound_id)
        if channel is not None and not _may_post(channel, me):
            return None, "", NO_PERMISSION
    return None, "", NO_CHANNEL


def describe_placeholders() -> list[dict]:
    """The tokens, as the console's page draws them."""
    return [{"name": "{%s}" % name, "what": what} for name, what in PLACEHOLDERS]


def _mentions(guild, *, ping: bool) -> tuple[object, str]:
    """What this message is allowed to mention, and the prefix that asks for it.

    Built by hand in both directions, and that is the whole point rather than
    tidiness: the message is a *template* with a server's own name interpolated
    into it, so a guild called ``@everyone`` would otherwise be able to mention
    everybody from inside a sentence nobody wrote as a ping. ``AllowedMentions``
    with a role and nothing else cannot do that, and ``AllowedMentions.none()``
    cannot do anything at all.
    """
    if ping:
        role = bot_channels.notify_role(guild, bot_channels.DEV_ROLE_ID)
        if role is not None:
            return _allowed(roles=[role]), f"{role.mention} "
    return _allowed(), ""


def _allowed(roles: object = False) -> object:
    """The allowance, with **every** field stated.

    All four fields are named, including the one this caller does not use, and
    ``roles`` has to *default* to nothing for the same reason: ``everyone``,
    ``roles``, ``users`` and ``replied_user`` all default to ``True`` in
    discord.py, so an allowance that only sets the field it means to use is not
    "only this role" — it is "this role, and also whatever the text happens to
    contain". For a *template* with a server's own name interpolated into it, that
    is a fleet-wide ping nobody asked for. Measured rather than read: the law in
    ``tests/test_broadcast.py`` that pins it failed on exactly those defaults the
    first time it ran.
    """
    import discord  # noqa: PLC0415 — imported here so the module stays importable without it

    return discord.AllowedMentions(
        everyone=False, roles=roles, users=False, replied_user=False
    )


async def run(
    client,
    template: str,
    *,
    send: bool = False,
    ping: bool = False,
    bot_name: str = "",
    spacing: float = SEND_SPACING_SECONDS,
) -> dict:
    """Plan the broadcast, and send it when ``send`` is true.

    One pass for both, so the preview the operator reads and the send they then
    perform cannot be two different pieces of arithmetic — and so a channel that
    changed between the two is resolved *again* rather than sent to from a plan
    that has gone stale.

    Returns a report rather than raising: a fleet write partly performed has to say
    what it did, which is the property ``utils.modules.set_many`` was written for
    on the server side of this same problem.
    """
    text = validate(template)
    name = bot_name or brand.NAME
    rows: list[dict] = []
    sent = failed = unreached = 0

    for guild in list(getattr(client, "guilds", None) or []):
        guild_id = getattr(guild, "id", None)
        members = getattr(guild, "member_count", None)
        # The four values the template may use, under exactly the names it uses:
        # ``{server}`` reads ``row["server"]``, which is why the row carries both
        # the placeholder names and the words this report is written in.
        values = {
            "server": str(getattr(guild, "name", "") or ""),
            "members": int(members) if isinstance(members, int) else None,
            "systems": systems_on(guild_id) if guild_id is not None else None,
            "bot": name,
        }
        row = {
            "guild_id": int(guild_id) if guild_id is not None else None,
            "name": values["server"],
            "members": values["members"],
            "systems": values["systems"],
            "bot": name,
        }
        channel_id = None
        if guild_id is not None:
            channel_id = await bot_channels.bound(guild_id, "update")
        channel, via, reason = _target(guild, channel_id)
        row.update({
            "channel_id": getattr(channel, "id", None),
            "channel_name": getattr(channel, "name", "") or "",
            "via": via,
            "reason": reason,
            "reachable": channel is not None,
            "text": render(text, values),
            "status": "would-send" if channel is not None else "unreached",
        })

        if channel is None:
            unreached += 1
            rows.append(row)
            continue
        if not send:
            rows.append(row)
            continue

        allowed, prefix = _mentions(guild, ping=ping)
        if sent or failed:
            await asyncio.sleep(spacing)
        try:
            await channel.send(content=prefix + row["text"], allowed_mentions=allowed)
        except Exception as exc:  # noqa: BLE001 — one server cannot stop the rest
            failed += 1
            row["status"] = "failed"
            row["reason"] = f"Discord refused it ({type(exc).__name__})"
        else:
            sent += 1
            row["status"] = "sent"
        rows.append(row)

    return {
        "send": bool(send),
        "ping": bool(ping),
        "bot": name,
        "template": text,
        "rows": rows,
        "sent": sent,
        "failed": failed,
        "unreached": unreached,
        "reachable": sum(1 for row in rows if row["reachable"]),
        "total": len(rows),
    }


def summarise(report: dict) -> str:
    """The sentence the console answers with, and the one the audit records.

    Written once because both print it, and a count that is described in two
    places is the drift this project keeps writing laws about.
    """
    total = report.get("total", 0)
    reachable = report.get("reachable", 0)
    if not total:
        return "The bot is not in any server, so there was nobody to tell."
    if not report.get("send"):
        return (
            f"This would reach {reachable} of {total} servers. Nothing was sent — "
            "the preview is the list, and the send is the button under it."
        )
    parts = [f"Sent to {report.get('sent', 0)} of {total} servers"]
    if report.get("failed"):
        parts.append(f"{report['failed']} refused by Discord")
    if report.get("unreached"):
        parts.append(f"{report['unreached']} could not be reached")
    return ". ".join(parts) + "."
