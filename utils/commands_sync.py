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

#: Keys whose list Discord **stores in its own order** rather than the order we
#: send it. discord.py declares a ``GuildChannel`` parameter's types in its own
#: union order — ``/hide``'s ``target`` goes out as ``[13, 2, 0, 5, 4, 15, 16]``
#: — and Discord hands them back sorted (``[0, 2, 4, 5, 13, 15, 16]``). Every
#: other list is compared element-wise and order-sensitively, which is right for
#: options and subcommands, so without this rule a command carrying one reads
#: back as stale on **every** boot and re-uploads itself forever, spending the
#: daily write budget on a change that can never be made. That is exactly the
#: permanently-out-of-date shape ``/structure`` was reported with (see
#: ``CHANGELOG.md``); ``/hide`` is the only command left that carries one.
UNORDERED_KEYS = frozenset({"channel_types"})

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


def _same_unordered(local, remote) -> bool:
    """Whether two lists hold the same items, in any order.

    For the keys in :data:`UNORDERED_KEYS` alone — everywhere else a list is
    compared element-wise, because that is how Discord stores options and
    subcommands. ``key=repr`` rather than a plain sort so a list that ever mixes
    types cannot raise where an answer of yes or no is the whole contract.
    """
    if not isinstance(local, (list, tuple)) or not isinstance(remote, (list, tuple)):
        return False
    if len(local) != len(remote):
        return False
    left = sorted(local, key=repr)
    right = sorted(remote, key=repr)
    return all(_same_value(one, two) for one, two in zip(left, right))


def matches(local, remote) -> bool:
    """Whether one locally-defined command matches its registered counterpart.

    Every key we *send* must be present and equal on the server; keys the server
    adds on its own, drops when default, or no longer returns at all are ignored
    (see the constants above). Lists are compared element-wise and
    order-sensitively, which is how Discord stores options and subcommands —
    except the keys in :data:`UNORDERED_KEYS`, which Discord sorts itself.
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
            if key not in remote:
                return False
            if key in UNORDERED_KEYS:
                if not _same_unordered(value, remote[key]):
                    return False
            elif not matches(value, remote[key]):
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
#: **Every name a `deleted_features/` cog ever declared belongs here**, and that
#: is a rule rather than a habit: archiving a cog removes it from the tree and
#: from nothing else. Discord keeps the registration, so the picker keeps
#: offering it, and a member who clicks it gets "An unexpected error occurred"
#: because the interaction arrives for a command the bot no longer has. That is
#: exactly how `/music` and `/play` — archived with the pre-stream player in
#: ``deleted_features/radio_2026_09/`` — were still sitting in **every** server's
#: picker, failing for anyone who tried them, months after the cogs behind them
#: were gone. ``tests/test_command_sync.py`` holds the rule now: no archived
#: cog's top-level command name may be left out of this set.
#:
#: ``upload``, ``upload-big`` and ``pin-upload-info`` were the big-file upload
#: feature, retired on 2026-09-14 (archived in ``deleted_features/``). Once a
#: deploy has deleted them this list is history rather than a to-do: it simply
#: stops matching anything — which is also true of the music player's names
#: below, the ones already swept. Being on the list is not a claim that Discord
#: still holds the name; it is permission to delete it if it does.
#:
#: The rest were **folded away and left behind in Discord**. `/dashboard` became
#: the ⚡ button on the `/help` panel; `/setavatar`, `/setbanner`, `/sync` and
#: `/updatedb` became `/bot avatar`, `/bot banner`, `/bot sync` and `/bot db`;
#: `/addlevelrole` and `/removelevelrole` became `/levelroles add` and
#: `/levelroles remove`; `/privacy` and `/tos` became `/legal`; `/meowfee`
#: became the fee view on the same panel as `/eco-admin fee`; and
#: `/activity-top` and `/activity-channel` became `/activity top` and
#: `/activity channel`. Folding them without this list is not harmless. Discord
#: allows an application **100 chat-input commands**, those twelve were still
#: holding slots, and with the ceiling reached every *new* command was refused
#: with **``30032 Maximum number of application commands reached``** — `/bot`
#: and `/legal` among them, which is why neither ever appeared in anyone's
#: picker. (`/activity` is deliberately *not* here: the group is that name now,
#: so it is an update, not a retirement — deleting it would take the group with
#: it.)
RETIRED_COMMANDS: frozenset[str] = frozenset(
    {
        "upload", "upload-big", "pin-upload-info",
        "dashboard", "setavatar", "setbanner", "sync", "updatedb",
        "addlevelrole", "removelevelrole", "privacy", "tos", "meowfee",
        "activity-top", "activity-channel",
        # The music player that `deleted_features/cogs/music.py` was: a queue in
        # one voice channel, replaced by `/radio`'s stations in
        # `deleted_features/radio_2026_09/` and then by `cogs/radio.py`. Both
        # archives are here, because both of them once registered commands.
        "play", "247", "pause", "resume", "skip", "stop", "queue",
        "nowplaying", "volume", "filter", "songinfo", "lyrics",
        # The other archived cogs, which draw their own groups.
        "reactionrole-add", "giveaway", "tempvoice", "welcome", "color",
        # Archived 2026-10-06, at the owner's request: Decat (the question that
        # travelled to the operator's own machine) and Alone Time (one voice
        # channel locked to the member holding it). Both drew a root command, so
        # both keep a registration in every server that ever had the system on
        # until this list deletes it — which is the whole reason the list is
        # checked against ``deleted_features/`` by `tests/test_command_sync.py`
        # now instead of against anybody's memory of what was removed.
        "decat", "alone",
        # Archived the same day, in the second pass: Starboard, Server Structure,
        # Voice Company and the legacy pair (Radio and Anonymous Q&A). Every one
        # of them drew a root command, and every one of those roots is still
        # registered in every server — the five names below are the only thing
        # that takes them out. The comments in ``deleted_features/README.md`` say
        # what each of them was; this list says that they are gone.
        "starboard", "structure", "company", "radio", "qna",
    }
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


def removable(registered, wanted, known) -> list[tuple[str, str]]:
    """``(name, id)`` for commands a scope is registered with and must not have.

    The narrow counterpart to :func:`retired`, and the rule that makes per-server
    command lists possible: a command may be taken out of **one scope** when that
    scope is not supposed to offer it — because the server turned the module off,
    or because the copy belongs in the per-server scope now and this is the stale
    global one.

    Three constraints, each from a failure this project already had, keep this
    from becoming the destructive overwrite it resembles:

    * ``known`` is the set of names the feature registry declares. A name nobody
      declares is *never* removed — a typo upstream cannot delete a command that
      is working, it can only leave one visible.
    * Only a name that is registered **and** absent from ``wanted`` is removed, so
      this can never take away something the scope is meant to keep.
    * The caller names the scope. Removing is per scope, so one server's choice
      cannot reach another server's commands.
    """
    have = index(registered or ())
    return [
        (name, str(payload["id"]))
        for name, payload in sorted(have.items())
        if name in known and name not in wanted and payload.get("id")
    ]


def prunable_globals(
    registered,
    wanted,
    known,
    held,
    wanted_by_guild,
) -> list[tuple[str, str]]:
    """``(name, id)`` for global copies that every server needing one holds.

    The narrow half of :func:`removable`, for the one scope whose contents are
    visible everywhere: a command that now belongs to each server individually
    must leave Discord's **global** lane, or it stays in the picker of every
    server that switched its system off — and appears twice in the servers that
    did not, because their own copy is registered under the same name.

    Deleting the global copy is only safe once each server that still wants the
    command has its own: a server whose publish has not landed yet would lose
    the command entirely in the window between the two, and *that* is the
    destructive-overwrite outcome the publisher is built to avoid. So the
    condition is proved per name instead of assumed from "every publish
    finished" — which is what let one unrelated command, stuck behind a spent
    daily write budget, hold the whole lane open indefinitely.

    ``held`` is each server's registered commands and ``wanted_by_guild`` the
    same servers' wanted names, in the same order. A guild that does not want
    the name is not asked to hold it: it is exactly the server the global copy
    is leaking into.
    """
    if registered is None or len(held) != len(wanted_by_guild):
        # A lane nobody read is not a lane anybody may guess about, and a
        # caller that handed over half a fleet cannot prove anything about the
        # half it left out.
        return []
    candidates = removable(registered, wanted, known)
    if not candidates:
        return []
    have = [index(payloads) for payloads in held]
    prunable: list[tuple[str, str]] = []
    for name, command_id in candidates:
        needs = [
            server for server, wants in enumerate(wanted_by_guild) if name in wants
        ]
        if all(name in have[server] for server in needs):
            prunable.append((name, command_id))
    return prunable


def stranded_globals(
    registered, wanted, known, held, wanted_by_guild
) -> list[str]:
    """The candidates :func:`prunable_globals` had to leave, by name.

    Reported rather than swallowed: these are the commands still visible in a
    server that switched them off, and the only thing standing between them and
    the delete is a server that has not published its own copy yet. A reader who
    can see which ones they are can tell "still publishing" from "Discord said
    no" — the distinction this whole module exists for.
    """
    if registered is None:
        return []
    prunable = {
        name
        for name, _id in prunable_globals(
            registered, wanted, known, held, wanted_by_guild
        )
    }
    return [
        name
        for name, _id in removable(registered, wanted, known)
        if name not in prunable
    ]


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
