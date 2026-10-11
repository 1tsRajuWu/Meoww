"""Which slash commands one server should have, and which it must not.

Discord offers exactly one mechanism for hiding a command in one server while
another server keeps it: **register the command per server**. A globally
registered command is visible in every server the bot is in, and no permission
bit, context or option can take it back — so a server that turns the market off
would otherwise keep seeing `/market` in its picker and keep getting "the market
is turned off on this server" when a member clicked it.

That makes this module the rule the publisher follows. It is pure on purpose: no
``discord`` import, no database, no I/O. The caller hands it the server's module
states (the same cache the commands themselves consult) and gets back the names
that server may have.

The gate for a command comes from ``features.MODULE_COMMAND_MAP`` — the registry
that already binds every command in the tree to the modules that must be on for
it to work, and that the test-suite already holds to the real command tree. The
one rule worth stating plainly: a name this module does not recognise is **not**
hidden. Failing open means a typo in the registry can only ever leave a command
visible, never delete one somebody is using.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from utils import features
from utils.permissions import VIP_ONLY_COMMANDS


def gate_of(command: str) -> tuple[str, ...]:
    """The modules that must all be on for ``command`` to work (``()`` = core)."""
    return features.MODULE_COMMAND_MAP.get(command, ())


def is_available(command: str, states: Mapping[str, bool]) -> bool:
    """Whether ``command`` belongs in a server whose modules read as ``states``.

    A missing entry falls back to the feature's own default, which is what
    ``modules.is_enabled`` does — so a server with no saved rows gets the same
    answer from the picker as it gets from the command's own check.
    """
    modules = gate_of(command)
    if not modules:
        # Core: help, legal, the bot's own group, server control. Always offered.
        return True
    return all(
        bool(states.get(module, features.default_enabled(module)))
        for module in modules
    )


def for_identity(names: Iterable[str], *, is_vip: bool | None) -> set[str]:
    """The names one bot identity may publish.

    The two bots split the tree: the VIP instance publishes only the VIP
    commands, and the main instance publishes everything else. That is what
    takes normals off the VIP picker and VIP off the main one, handing both
    scopes their ceiling slots back. ``None`` (tests, dry runs, pre-login)
    keeps everything — only a logged-in instance may narrow a scope.
    """
    names = set(names)
    if is_vip is True:
        names &= VIP_ONLY_COMMANDS
    elif is_vip is False:
        names -= VIP_ONLY_COMMANDS
    return names


def global_names(names: Iterable[str]) -> set[str]:
    """The commands that belong in Discord's **global** scope.

    A globally-registered command is visible in every server the bot is in and no
    permission bit, context or option narrows it per server — so the shared lane
    is only for what has to work with no server to resolve it: the help panel and
    the policies, which are also the two Discord lets a member run in a DM. Every
    other command is registered per server (see ``bot.py``), and this is the one
    rule both the publisher and ``force_sync.py`` trim the shared lane by.
    """
    return {
        name for name in names
        if name in features.DM_ALLOWED_COMMANDS
        and name not in features.MODULE_COMMAND_MAP
    }


def available_names(names: Iterable[str], states: Mapping[str, bool]) -> list[str]:
    """The subset of ``names`` this server should have, order preserved."""
    return [name for name in names if is_available(name, states)]


def hidden_names(names: Iterable[str], states: Mapping[str, bool]) -> list[str]:
    """The subset of ``names`` this server's own modules turn off.

    Only commands the registry knows can be named here, so "hidden" is always a
    decision somebody made by switching a module off — never the absence of a
    name from a local tree, which is the bulk-overwrite mistake this project
    already lived through once.
    """
    return [
        name for name in names
        if name in features.MODULE_COMMAND_MAP and not is_available(name, states)
    ]


def unknown(names: Iterable[str]) -> list[str]:
    """Names the registry knows nothing about at all — reported, never hidden.

    A *core* command (``help``, ``dbstatus``) is known: it is declared
    always-allowed, and it is available however the modules are set. A name that
    is in neither list is a typo or a command nobody has declared yet, and that
    is the one case where this rule deliberately fails open — it can leave a
    command visible, and can never take one away.
    """
    return [
        name for name in names
        if name not in features.MODULE_COMMAND_MAP
        and name not in features.ALWAYS_ALLOWED_COMMANDS
    ]


def summarise(names: Iterable[str], states: Mapping[str, bool]) -> dict:
    """Counts for the dashboard, computed from the same rule as the publisher."""
    names = list(names)
    off = hidden_names(names, states)
    return {
        "total": len(names),
        "available": len(names) - len(off),
        "hidden": len(off),
        "hidden_names": sorted(off),
        "modules": {
            feature.id: {
                "name": feature.name,
                "icon": feature.icon,
                "enabled": bool(
                    states.get(feature.id, feature.enabled_by_default)
                ),
                "commands": [
                    name for name in names if feature.id in gate_of(name)
                ],
            }
            for feature in features.FEATURES
            if feature.toggleable
        },
    }
