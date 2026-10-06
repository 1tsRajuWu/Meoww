"""Load every cog for real and compare the actual command tree with the
registry.

This is the strongest drift guard in the suite: if someone adds a slash command
and forgets to gate it, or retires a feature and leaves its commands behind,
these tests fail instead of the mismatch silently shipping."""
import unittest
from pathlib import Path

import discord
from discord.ext import commands

from utils import features

ROOT = Path(__file__).resolve().parent.parent


def _commands_defined_in_bot_py() -> set[str]:
    """Commands registered directly in bot.py (not through a cog)."""
    import re

    source = (ROOT / "bot.py").read_text()
    return set(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', source))


class TestCommandTree(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")
        self.registered = {c.name for c in self.bot.tree.get_commands()}
        self.subcommands = {
            c.name: tuple(s.name for s in c.commands)
            for c in self.bot.tree.get_commands()
            if isinstance(c, discord.app_commands.Group)
        }

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_command_tree_is_not_empty(self):
        self.assertGreater(len(self.registered), 50)

    def test_no_two_commands_share_a_name(self):
        """"Is there a double command?" is a question worth answering in code.

        Two readings, and only one of them is a defect — so this pins the one that
        is. A name registered **twice in one scope** is the defect: Discord keys a
        command by its name, so the second registration replaces the first and the
        handler behind it becomes unreachable, silently. The same rule applies
        inside a group, where two `status` verbs under `/bot` would be one command
        and one orphan.

        The other reading — an older copy left behind in a *different* scope — is
        not visible from here and is not this test's business. It is what
        `/dbstatus` became the day it folded into `/bot`: no such command in the
        tree, while every server still held a registration for one. That one is the
        sync lane's, and the changelog is where its fate is written down.
        """
        names = [command.name for command in self.bot.tree.get_commands()]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(duplicates, [], f"the tree registers these names twice: {duplicates}")

        for parent, children in self.subcommands.items():
            with self.subTest(group=parent):
                repeated = sorted({name for name in children if children.count(name) > 1})
                self.assertEqual(
                    repeated, [], f"/{parent} registers these verbs twice: {repeated}"
                )

    def test_every_gated_command_exists(self):
        missing = sorted(set(features.MODULE_COMMAND_MAP) - self.registered)
        self.assertEqual(missing, [], f"registry gates commands that don't exist: {missing}")

    def test_no_command_is_left_ungated(self):
        """A new command must be registered in the feature registry (or be core)."""
        ungated = sorted(
            self.registered - set(features.MODULE_COMMAND_MAP) - features.ALWAYS_ALLOWED_COMMANDS
        )
        self.assertEqual(
            ungated, [],
            f"commands missing from the feature registry: {ungated}",
        )

    def test_core_command_list_has_no_phantoms(self):
        """Every always-allowed name must be a real command."""
        known = self.registered | _commands_defined_in_bot_py()
        phantom = sorted(features.ALWAYS_ALLOWED_COMMANDS - known)
        self.assertEqual(phantom, [], f"always-allowed list names non-existent commands: {phantom}")

    def test_bot_py_commands_are_covered(self):
        defined = _commands_defined_in_bot_py()
        uncovered = sorted(defined - features.ALWAYS_ALLOWED_COMMANDS - set(features.MODULE_COMMAND_MAP))
        self.assertEqual(uncovered, [], f"commands in bot.py that are neither gated nor core: {uncovered}")

    def test_the_tree_stays_within_discords_command_cap(self):
        """Discord allows 100 global CHAT_INPUT commands, and the same per guild.

        Subcommands nest inside their parent command and don't cost anything, but
        every top-level command spends one of the 100 — and going over makes
        Discord reject the whole sync, which is how commands silently stop
        appearing. Keep some headroom rather than discovering this at the cap.
        """
        self.assertLessEqual(
            len(self.registered), 100,
            f"{len(self.registered)} top-level commands; Discord's cap is 100",
        )

    def test_the_bot_group_carries_the_commands_it_absorbed(self):
        """/bot exists so four commands cost one slot: a subcommand going missing
        would take away something an administrator reaches for."""
        self.assertEqual(
            set(self.subcommands.get("bot", ())),
            # `status` is the newest: the bot's own activity, editable at runtime
            # so changing a line does not need a redeploy. It is one subcommand
            # with optional options rather than five, because these slots are a
            # budget — see the test below.
            # `dbstatus` arrived the way this law says the next command has to:
            # `/dbstatus` folded into the group that already exists, because the
            # tree was at Discord's hundred and a report is the cheapest thing to
            # move. `decat` joined it for the same reason — the machine half of
            # that feature belonged to the operator rather than to any server's
            # members — and it left when Decat was archived, which is the same
            # budget seen from the other side: a slot given back, not spent.
            # `legacy` left the same way on 2026-10-06, with the two systems it
            # switched: the switch was a command, and a command for a system the
            # bot no longer has is a slot spent on nothing.
            {
                "avatar", "banner", "bug", "db", "dbstatus",
                "plan", "push", "status", "sync",
            },
        )

    def test_the_tree_stays_within_discords_cap(self):
        """The cap is a hundred, and the tree has been *at* it — so the next command merges.

        It sat at ninety-nine with one slot free until Alone Time took it: one
        root with seven verbs is one slot, and the alternative was a nested
        subgroup, which ``utils.permissions.walk`` cannot see and which would
        therefore escape the audience audit. That trade was deliberate, and this
        is the record of it — a hundred is Discord's own ceiling, and it refuses
        the *next* command outright, which is not hypothetical: this cog once
        failed to load because the tree went over.

        Decat was the next command, and the law had its first real exercise:
        rather than make room by inventing a nested subgroup, ``/dbstatus`` folded
        into ``/bot`` (``/bot dbstatus``), which freed a slot without hiding
        anything from the audit. A command added from here still has to be a verb
        on a root that already exists, or pay for itself the same way — and the
        tree is at **ninety-eight** now, because both of the systems that took
        the last slots have since been archived. The freed slots are the point
        worth reading twice: they are not a budget to spend casually, they are
        what a `deleted_features/` retirement gives back.
        """
        self.assertLessEqual(
            len(self.registered), 100,
            f"{len(self.registered)} top-level commands — Discord allows a hundred",
        )

    def test_sync_is_itself_a_registered_command(self):
        """Regression: the manual repair path has to stay reachable.

        The live guild was missing 21 of its 92 commands with `/sync` among them,
        which left no way to register anything from inside Discord. The bot now
        syncs on boot, but the manual fallback must stay reachable — it lives in
        the `/bot` group, which is a group precisely so that it costs one slot
        instead of four.
        """
        self.assertIn("bot", self.registered)
        self.assertIn("sync", self.subcommands.get("bot", ()))


if __name__ == "__main__":
    unittest.main()
