"""Compare the local slash-command tree with what Discord already has registered.

Discord enforces a **200-command-writes-per-day, per-guild** budget, and the
failure mode when it runs out is error 30034 — after which *nothing* registers and
it looks exactly like "the new command never shows up". A bot that blindly
re-uploads its whole tree on every boot walks into that: a handful of deploys in
one day is enough to exhaust it.

The safe pattern is **read → compare → write only if something differs**. These
helpers are pure (no ``discord`` import) so ``bot.py``, ``force_sync.py`` and the
test-suite all compare the same way.
"""
from __future__ import annotations

#: Keys Discord attaches when it stores a command. We never send them, and they
#: are not part of "is this command the same?", so they are ignored.
SERVER_ONLY_KEYS = frozenset({
    "id", "application_id", "guild_id", "version", "type",
    "name_localizations", "description_localizations",
})

#: Keys Discord **omits from its response when they still hold the default** — it
#: simply doesn't echo them back. discord.py always sends them, so without this
#: rule every unchanged command looks different and re-uploads the whole tree on
#: every boot, spending the write budget on nothing. Verified against live
#: payloads: `options: []`, `option.required: false`, `nsfw: false`.
DEFAULTED_KEYS = frozenset({"options", "required", "nsfw"})

#: Keys Discord *resolves* for us. We send null (discord.py's default) and it
#: stores its own value — an app installed to guilds and users comes back as
#: `integration_types: [0, 1]`, which would otherwise look like a permanent
#: difference. They are app-level settings, not per-command ones, so a null from
#: us is never a change.
RESOLVED_KEYS = frozenset({"contexts", "integration_types"})

#: Deprecated fields discord.py still sends but Discord no longer returns.
IGNORED_KEYS = frozenset({"dm_permission"})

#: Unicode variation selectors (U+FE00–U+FE0F). Discord **strips these when it
#: stores a command**, so a choice named `"✂️ Scissors"` comes back as
#: `"✂ Scissors"`. Without this the command looked permanently out of date — the
#: live application reported `/rps`, `/leaderboard` and `/roulette` as changed on
#: every single boot, which spends the daily write budget re-uploading a change
#: that can never be made. Stripping is safe: a real emoji edit still differs.
_VARIATION_SELECTORS = str.maketrans({chr(code): None for code in range(0xFE00, 0xFE10)})


def _without_variation_selectors(value: str) -> str:
    return value.translate(_VARIATION_SELECTORS)


def _is_empty(value) -> bool:
    """``None``, an empty container or ``False`` — what Discord would omit."""
    if value is None or value is False:
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _same_value(local, remote) -> bool:
    """Whether one leaf value matches, allowing for Discord's string encoding.

    Discord returns snowflake-shaped values as **strings** while discord.py sends
    integers: `/eco-admin`'s `default_member_permissions` goes out as ``8`` and
    comes back as ``"8"``. Comparing those directly made every permission-gated
    command look permanently out of date — verified live, where exactly the eight
    gated commands (`dashboard`, `leaderboard`, `addmoney`, `removemoney`,
    `setmoney`, `addbank`, `roulette`, `rps`) re-uploaded on *every* boot and
    spent the daily write budget on a change that never happened. Strings are also
    compared without their variation selectors, which Discord drops (see below).
    """
    if local == remote:
        return True
    if isinstance(local, bool) or isinstance(remote, bool):
        return False
    if isinstance(local, int) and isinstance(remote, str):
        return str(local) == remote
    if isinstance(local, str) and isinstance(remote, int):
        return local == str(remote)
    if isinstance(local, str) and isinstance(remote, str):
        return _without_variation_selectors(local) == _without_variation_selectors(remote)
    return False


def matches(local, remote) -> bool:
    """Whether one locally-defined command matches its registered counterpart.

    Every key we *send* must be present and equal on the server; keys the server
    adds on its own, drops when default, or no longer returns at all are ignored
    (see the constants above). Lists are compared element-wise and
    order-sensitively, which is how Discord stores options and subcommands.
    """
    if isinstance(local, dict):
        if not isinstance(remote, dict):
            return False
        for key, value in local.items():
            if key in SERVER_ONLY_KEYS or key in IGNORED_KEYS or key in RESOLVED_KEYS:
                continue
            if key in DEFAULTED_KEYS and _is_empty(value):
                # Empty and absent mean the same thing to Discord — but a value
                # where we sent none is still a difference.
                if not _is_empty(remote.get(key)):
                    return False
                continue
            if value is None:
                # A null we send is stored as an absent key.
                if remote.get(key) is None:
                    continue
                return False
            if key not in remote or not matches(value, remote[key]):
                return False
        return True
    if isinstance(local, list):
        if not isinstance(remote, list) or len(local) != len(remote):
            return False
        return all(matches(one, two) for one, two in zip(local, remote))
    return _same_value(local, remote)


#: Command names this bot published in the past and no longer has. The sync can
#: otherwise only add or replace (see ``bot.py``), which is what keeps a spent
#: write budget from taking commands away — and is also why a retired command
#: would stay in Discord's picker forever, failing for whoever clicks it. These
#: names, and only these, may be deleted: an explicit list, never "anything that
#: is not in the local tree", which is the bulk-overwrite instinct that once
#: cleared this guild down to a single command.
#:
#: ``upload``, ``upload-big`` and ``pin-upload-info`` were the big-file upload
#: feature, retired on 2026-09-14 (archived in ``deleted_features/``). Once a
#: deploy has deleted them this list is history rather than a to-do: it simply
#: stops matching anything.
RETIRED_COMMANDS: frozenset[str] = frozenset(
    {"upload", "upload-big", "pin-upload-info"}
)


def retired(registered) -> list[tuple[str, str]]:
    """``(name, id)`` for every retired command Discord still has registered."""
    found: list[tuple[str, str]] = []
    for command in registered or ():
        if not isinstance(command, dict):
            continue
        if command.get("name") in RETIRED_COMMANDS and command.get("id"):
            found.append((command["name"], str(command["id"])))
    return found


def index(commands) -> dict[str, dict]:
    """``{name: payload}`` for a list of command payloads."""
    return {
        command["name"]: command
        for command in commands
        if isinstance(command, dict) and command.get("name")
    }


def diff(local, remote) -> tuple[list[str], list[str]]:
    """Return ``(missing, stale)`` command names.

    ``missing``  — Discord has never seen it.
    ``stale``    — registered under the same name, but the content differs (a
                   changed description, a new option, a renamed subcommand).

    Both need a write; everything else must be left alone so the daily budget is
    spent only on real changes.
    """
    registered = index(remote)
    missing: list[str] = []
    stale: list[str] = []
    for command in local:
        name = command.get("name")
        if not name:
            continue
        if name not in registered:
            missing.append(name)
        elif not matches(command, registered[name]):
            stale.append(name)
    return missing, stale


def is_up_to_date(local, remote) -> bool:
    """True when Discord's copy already matches ours, so no write is needed."""
    missing, stale = diff(local, remote)
    return not missing and not stale


def summary(local, remote) -> str:
    """A one-line, human-readable description of the difference."""
    missing, stale = diff(local, remote)
    if not missing and not stale:
        return f"{len(local)} command(s) already up to date"
    parts = []
    if missing:
        parts.append(f"{len(missing)} missing ({', '.join(sorted(missing))})")
    if stale:
        parts.append(f"{len(stale)} out of date ({', '.join(sorted(stale))})")
    return "; ".join(parts)
