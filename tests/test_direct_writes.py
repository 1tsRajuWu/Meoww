"""Every cog that writes to a Discord object directly has to say why.

This is the part of the old ``tests/test_guardrails.py`` that outlived Server
Structure. That suite held two things: the machinery of the management tier — the
confirmation ceilings, the sizes, the structure door — and this one law, which is
about the *whole tree* rather than about any one system. The first half went to
``deleted_features/tests/test_guardrails.py`` with the feature; this half stayed,
because it is the law that ages well: a pinned inventory of every file in
``cogs/`` that changes something a person would notice, so a **new** direct write
cannot appear without somebody having to write down why.

Read as a source-level property rather than as a promise in a docstring: the
inventory is compared against the files themselves, so a cog that grows a
``channel.delete()`` fails here whether or not anybody remembered to update a
list.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COGS = ROOT / "cogs"

#: The methods that change something a person would notice. Attribute names rather
#: than an allow-list of receivers, because the point is to catch the call whatever
#: it was called on.
WRITES = frozenset({
    "delete", "create_text_channel", "create_voice_channel", "create_role",
    "create_category", "create_automod_rule", "remove_roles", "ban", "kick",
    "timeout", "edit", "purge", "set_permissions", "clone",
})

#: Objects that may be written to without going through a door: the doors
#: themselves, the database, and the library's own classes. Everything else is a
#: direct change to somebody's server.
DOORS = frozenset({"moderation_actions", "db", "discord", "discord_utils"})

#: Every file in ``cogs/`` that still writes to a Discord object itself, with the
#: reason it is allowed to. Pinned rather than derived: this list is the record of
#: a decision, and a *new* entry means somebody added a write that skips the door.
#: The entries marked **pre-existing** are named as gaps rather than blessed — the
#: ticket lifecycle is a structure change that predates the structure door, and
#: closing it is its own pass. The lockdown's permission edits *were* the other
#: gap and are closed: `/lockdown`, `/unlock`, `/lockdown-server` and
#: `/unlock-server` all go through ``utils/server_control``, which writes down what
#: each change replaced before it makes it, so what is left in the cog is the
#: `/slowmode` value and the message a locked channel's listener removes.
DIRECT_WRITES = {
    "automod.py": [
        "message.delete()",      # a message, not server state; automod records its own actions
        "author.timeout()",      # recorded through utils.moderation_actions
        # Discord's own AutoMod rules, and only the ones this bot created —
        # `_owns_native_rule` refuses every rule whose creator is not the bot, so
        # what the sync writes over is configuration the bot owns, not a server's.
        "guild.create_automod_rule()",
        "rule.edit()",
    ],
    "bot_config.py": [
        "user.edit()",           # the bot's own profile, not a server's
    ],
    "moderation.py": [
        "channel.purge()",       # /purge removes messages, not the channel
    ],
    "reaction_roles.py": [
        # The feature *is* the role change: a member takes a role by reacting,
        # with nobody to confirm it and no case to open, so there is no door to
        # route it through — what it keeps instead is the guard `/addrole` uses
        # (`permissions.role_grant_problem` on the person wiring the mapping up,
        # and the bot's own hierarchy on the one Discord lets it hand out), and a
        # refusal is written to the host log rather than swallowed.
        "target.remove_roles()",
    ],
    "server_control.py": [
        "channel.edit()",        # pre-existing: /slowmode
        "member.remove_roles()",  # pre-existing: /defaultrole clear
        # The lockdown's teeth. A permission cannot stop an administrator, so
        # anything posted into a locked channel is removed instead: it is a
        # message rather than server state, the removal goes to the host log, and
        # which channels are locked is decided by the same ledger the undo reads
        # (``utils.server_control``). The permission edits themselves moved into
        # that module, which is what closed this file's other pre-existing gap.
        "message.delete()",
        # A burst past the lock earns a timeout, and this one is deliberately not
        # routed through ``moderation_actions.apply``: that door refuses to act on
        # an administrator, which is the whole thing this rule exists to ignore.
        # The case and the log card still go through ``moderation_actions.record``
        # and ``modlog.post``, so a rank-free timeout is a recorded one.
        "member.timeout()",
    ],
    "social.py": [
        "msg.edit()",            # the bot editing its own embed
    ],
    "tickets.py": [
        "channel.delete()",           # pre-existing: the ticket channel lifecycle
        "channel.edit()",             # pre-existing
        "channel.set_permissions()",  # pre-existing
        "guild.create_category()",    # pre-existing
        "guild.create_text_channel()",  # pre-existing
    ],
}


def _receiver(call: ast.Call) -> str:
    """How a call's receiver is spelled — a name, or the attribute it came from."""
    value = call.func.value
    return (
        getattr(value, "id", None)
        or getattr(value, "attr", None)
        or type(value).__name__
    )


def direct_writes(path: Path) -> list[str]:
    """``receiver.method()`` for every write in one file that skips the doors."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in WRITES:
            continue
        receiver = _receiver(node)
        if receiver in DOORS:
            continue
        found.append(f"{receiver}.{node.func.attr}()")
    return found


class NoNewDirectWriteWithoutAReason(unittest.TestCase):

    def test_no_new_file_writes_to_a_server_without_saying_why(self):
        """The inventory is the record of a decision, and it may only be added to.

        A new entry is somebody skipping the door — the failure this whole group
        exists to make loud instead of silent.
        """
        inventory = {
            path.name: direct_writes(path)
            for path in sorted(COGS.glob("*.py"))
            if direct_writes(path)
        }
        pinned = {name: sorted(set(calls)) for name, calls in DIRECT_WRITES.items() if calls}
        self.assertEqual(
            sorted(inventory),
            sorted(pinned),
            "a cog writes to Discord directly and is not in the pinned inventory",
        )
        for name, calls in pinned.items():
            self.assertEqual(
                sorted(set(inventory.get(name, []))),
                calls,
                f"{name} gained or lost a direct write",
            )


if __name__ == "__main__":
    unittest.main()
