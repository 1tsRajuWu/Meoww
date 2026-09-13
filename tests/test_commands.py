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

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_command_tree_is_not_empty(self):
        self.assertGreater(len(self.registered), 50)

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


if __name__ == "__main__":
    unittest.main()
