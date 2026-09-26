"""What this server's dashboard should say when something is quietly not working.

Every switch on a server's dashboard can read *on* while the feature behind it
does nothing at all, and the reason is always a fact the bot already knows and
never says out loud:

* the moderator log points at a channel somebody deleted, so every case this
  server's moderators open lands nowhere — and the only trace is one line in a
  host log nobody reads;
* the bot's own role is missing a permission a switched-on system works through,
  so the command answers *missing permission* at the moment somebody needs it;
* the default role sits above the bot's own role in the hierarchy, so
  ``/defaultrole`` reports success and hands out nothing;
* Discord is holding a command list from before the last switch was flipped, so
  a command this page says is on is not offered in the server.

Each of those is a *reading* rather than a behaviour, and a reading belongs on
the page an administrator opens when something looks wrong. So the checks live
here — pure functions over plain facts — and not in the view that draws them:
this project's stand-in-driven tests can exercise a function of data without a
gateway, and the version of this decision that lives inside a route is the
version that only ever gets tested through the page it happens to draw.

**What is not here is as deliberate as what is.** This module does not guess at
the server's *intent*: a system that is switched off is not a finding, and a
setting that was never touched is not one either. A list of everything a server
could configure would be a permanent noise floor, and a card nobody can bring
themselves to read is the failure mode the whole project keeps writing down. So
each check answers one question — *is this, right now, costing this server
something it asked for?* — and the empty answer is a real one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Most serious first. A **broken** row is something that is not working now; a
#: **warning** is something that is not right but costs nothing until somebody
#: reaches for the feature. The card sorts by this, because a page whose first
#: row is the least of its problems is a page read from the top and abandoned.
SEVERITIES = ("broken", "warning")
RANK = {name: index for index, name in enumerate(SEVERITIES)}

#: The permissions the bot itself works through, checked against Discord's own
#: answer for its member (`guild.me.guild_permissions`). Two, because a card is
#: what this bot says everything with: no ``send_messages`` is a bot that cannot
#: answer at all, and no ``embed_links`` is one whose every answer arrives as the
#: fallback text or not at all — which looks, from inside the server, like a bot
#: that is simply broken.
POSTING_NEEDS = (
    ("send_messages", "say anything at all in this server"),
    ("embed_links", "post the cards every answer of its is made of"),
)

#: What each switched-on system *works through*, and what is lost without it.
#: Read from the bot's own role rather than any channel overwrite, because that
#: is the permission Discord applies wherever a channel does not say otherwise —
#: the case a server hits by never having configured the channel at all.
#:
#: Only systems with a permission that is genuinely load-bearing are listed. A
#: system whose commands need nothing beyond posting is absent on purpose: a
#: check that fires on a healthy server is a check somebody deletes.
NEEDS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "moderation": (
        ("manage_messages", "delete a message, and run the filters that catch one"),
        ("kick_members", "kick a member"),
        ("ban_members", "ban one"),
        ("moderate_members", "time one out"),
    ),
    "structure": (
        ("manage_channels", "lock, unlock and slow a channel down"),
        ("manage_roles", "give a member the roles its panels name"),
    ),
    "tickets": (
        ("manage_channels", "open a channel for a ticket"),
    ),
}

#: Every permission name this module asks about, so the caller can read one dict
#: off Discord instead of a second copy of this table.
PERMISSION_NAMES = tuple(
    sorted({name for needs in NEEDS.values() for name, _lost in needs}
           | {name for name, _lost in POSTING_NEEDS})
)

#: What each reading is *called* on the card, and the reason this module has an
#: opinion about it: a card that could not read something says which reading it
#: could not take. "Nothing needs a look" over a check that never ran is exactly
#: the failure this whole page exists to catch.
READINGS = (
    ("permissions", "the bot's own permissions here"),
    ("log", "the moderator log"),
    ("default_role", "the default role"),
    ("publish", "Discord's copy of this server's commands"),
)


@dataclass(frozen=True)
class Finding:
    """One thing worth an administrator's attention, and what to do about it.

    ``key`` is stable and carries the subject — ``log:gone``, ``perm:moderation:kick_members``
    — so a test can name the row it means, and so the same problem on two servers
    is one problem in a log rather than two prose sentences.
    """

    key: str
    severity: str
    title: str
    detail: str
    fix: str


def findings(facts: Mapping) -> list[Finding]:
    """Everything wrong with this server, worst first, from facts already read.

    ``facts`` is deliberately a mapping rather than a dozen named arguments: the
    caller reads what it can and passes what it has, and a reading it could not
    take is a key that is absent or ``None`` — which is *not* the same as a
    reading that came back healthy, and :func:`unread` is where that difference is
    kept.
    """
    found: list[Finding] = []
    found += _posting_findings(facts)
    found += _permission_findings(facts)
    found += _log_findings(facts)
    found += _default_role_findings(facts)
    found += _publish_findings(facts)
    # Severity only, and sorted once: Python's sort is stable, so the rows keep
    # the order the checks are declared in, which is the order a person reads
    # them — the bot's own permissions, then the moderator log, then the default
    # role, then the command list. Ordering by the key instead would put
    # ``commands:stale`` above ``log:unset`` because of the alphabet.
    return sorted(found, key=lambda row: RANK.get(row.severity, 99))


def _posting_findings(facts: Mapping) -> list[Finding]:
    permissions = facts.get("permissions")
    if not permissions:
        return []
    lost = [
        (name, what) for name, what in POSTING_NEEDS
        if not _allowed(permissions, name)
    ]
    if not lost:
        return []
    # One row rather than one per permission: the remedy is one action — fix the
    # bot's role — and two rows saying "the same thing is wrong, again" is how a
    # card buries the finding beside it.
    return [
        Finding(
            key="posting",
            severity="broken",
            title="The bot cannot post properly in this server",
            detail=(
                "Its own role is missing "
                + _and_list([f"{_permission_label(name)} (to {what})" for name, what in lost])
                + "."
            ),
            fix=(
                "In Server Settings → Roles, give the bot's role those permissions — "
                "or move the bot's role up if a channel overwrite is taking them away. "
                "Discord applies the role wherever a channel does not say otherwise."
            ),
        )
    ]


def _permission_findings(facts: Mapping) -> list[Finding]:
    permissions = facts.get("permissions")
    if not permissions:
        return []
    systems = facts.get("systems") or {}
    names = facts.get("names") or {}
    found = []
    for module_id, needs in NEEDS.items():
        if not systems.get(module_id):
            continue  # switched off: a gap here costs this server nothing today
        for permission, what in needs:
            if _allowed(permissions, permission):
                continue
            label = str(names.get(module_id) or module_id)
            found.append(
                Finding(
                    key=f"perm:{module_id}:{permission}",
                    severity="broken",
                    title=f"The bot cannot {what}",
                    detail=(
                        f"**{label}** is switched on here, and the bot's own role is "
                        f"missing {_permission_label(permission)}, so that is what "
                        "happens the moment somebody needs it."
                    ),
                    fix=(
                        f"Give the bot's role {_permission_label(permission)} in Server "
                        "Settings → Roles, or switch the system off if this server does "
                        "not want it."
                    ),
                )
            )
    return found


def _log_findings(facts: Mapping) -> list[Finding]:
    systems = facts.get("systems") or {}
    log = facts.get("log")
    if log is None or not systems.get("moderation"):
        return []
    if not log.get("configured"):
        return [
            Finding(
                key="log:unset",
                severity="warning",
                title="Moderation has nowhere to log",
                detail=(
                    "Every action taken here — a moderator's own, and the automatic "
                    "ones — is supposed to leave a case in a log channel. None is set, "
                    "so the record is the reply the command gave and nothing else."
                ),
                fix=(
                    "Set a moderator log channel on the Server page (or with `/modlog`), "
                    "or switch **Moderation & Logs** off if this server keeps its "
                    "records elsewhere."
                ),
            )
        ]
    if not log.get("exists"):
        return [
            Finding(
                key="log:gone",
                severity="broken",
                title="The moderator log points at a channel that is gone",
                detail=(
                    f"This server logs moderation to {log.get('channel') or 'a channel'}, "
                    "which no longer exists. Every case opened since it was deleted has "
                    "landed nowhere."
                ),
                fix=(
                    "Pick another channel on the Server page (or with `/modlog`). The "
                    "cases themselves are still in the database — `/modlog view` reads "
                    "them — but nothing new will be written down until this is set."
                ),
            )
        ]
    if log.get("can_post") is False:
        return [
            Finding(
                key="log:blocked",
                severity="broken",
                title="The bot cannot write to the moderator log",
                detail=(
                    f"The log channel is {log.get('channel') or 'set'}, and the bot's "
                    "own role cannot post the cards a case is written as there."
                ),
                fix=(
                    "Give the bot's role **View Channel**, **Send Messages** and **Embed "
                    "Links** on that channel, or pick one it can already write to."
                ),
            )
        ]
    return []


def _default_role_findings(facts: Mapping) -> list[Finding]:
    role = facts.get("default_role")
    if role is None or not role.get("configured"):
        return []
    if not role.get("found"):
        return [
            Finding(
                key="default_role:gone",
                severity="broken",
                title="The default role no longer exists",
                detail=(
                    f"New members are supposed to be given `{role.get('name') or 'a role'}` "
                    "on arrival, and that role is not in this server any more."
                ),
                fix=(
                    "Set the default role again with `/defaultrole set`, or clear it with "
                    "`/defaultrole clear` so nothing is promised."
                ),
            )
        ]
    if role.get("above") is False:
        return [
            Finding(
                key="default_role:hierarchy",
                severity="broken",
                title="The default role is above the bot's own role",
                detail=(
                    f"New members are supposed to be given `{role.get('name')}`, and "
                    "Discord refuses a role change for a role that sits at or above the "
                    "bot's highest one — so the bot reports that it assigned it while "
                    "nothing changes on the member."
                ),
                fix=(
                    "Drag the bot's own role above that role in Server Settings → Roles, "
                    "or pick a lower role with `/defaultrole set`."
                ),
            )
        ]
    return []


def _publish_findings(facts: Mapping) -> list[Finding]:
    publish = facts.get("publish")
    if not publish or publish.get("matches") is not False:
        return []
    return [
        Finding(
            key="commands:stale",
            severity="warning",
            title="Discord is holding a different command list than this server runs",
            detail=str(publish.get("text") or "").strip()
            or "The last publish left Discord's copy of this server's commands disagreeing "
            "with its own switches.",
            fix=(
                "Open the Commands page and publish again. Until then, a command this "
                "page says is on may not be offered in the server at all."
            ),
        )
    ]


def presets() -> dict:
    """The facts this module can check, for a caller that has read none of them.

    Not a default to fall back on but a *shape* to fill in, so a view that reads
    two of the four readings cannot mistake the two it skipped for the two that
    came back clean — :func:`unread` is what names them.
    """
    return {
        "systems": {},
        "names": {},
        "permissions": None,
        "log": None,
        "default_role": None,
        "publish": None,
    }


def read(facts: Mapping) -> list[str]:
    """The readings that were actually taken, in the order the card names them."""
    return [label for key, label in READINGS if facts.get(key) is not None]


def unread(facts: Mapping) -> list[str]:
    """The readings that could not be taken, so the card can say so.

    This is the difference the whole module turns on: a check nobody could
    perform and a check that came back healthy both produce no findings, and a
    page that cannot tell them apart prints *"nothing needs a look"* over a
    server it never looked at.
    """
    return [label for key, label in READINGS if facts.get(key) is None]


def summary(rows) -> dict:
    """The card's own counts, from the rows it is about to draw."""
    counted = {"broken": 0, "warning": 0}
    for row in rows:
        counted[row.severity] = counted.get(row.severity, 0) + 1
    return counted


def _allowed(permissions: Mapping, name: str) -> bool:
    """Whether Discord said yes, treating a reading it did not answer as yes.

    A permission the stand-in or the library did not hand over is not a finding:
    the alternative is a card that reports a missing permission every time a fake
    gateway is missing one, which is the shape of test that teaches nothing.
    """
    value = permissions.get(name)
    return True if value is None else bool(value)


def _permission_label(name: str) -> str:
    """``kick_members`` as Discord's own words: ``Kick Members``."""
    return " ".join(part.capitalize() for part in name.split("_"))


def _and_list(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` — never ``a, b, c`` ending in a comma."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
