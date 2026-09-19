"""Which commands one server has, and the invariant that makes it safe to hide.

Discord cannot hide a globally-registered command in one server, so a server
that turns the market off kept seeing ``/market`` in its picker and kept being
told the market was off when a member clicked it. The fix is to register each
server's own list (see ``bot.py``), and this module pins the rule that decides
that list and the property that makes it honest:

    **every command a server loses is one the bot would have refused there.**

If that were not true, hiding would be a new rule bolted beside the module
gates, and the two could drift — a command that works, invisible; or a command
that never works, still on offer. So the test does not read the registry and
trust it: it loads the real cogs, calls each cog's real ``interaction_check``
with every module off, and asserts that the set it refuses is *exactly* the set
the rule hides.
"""
import inspect
import unittest
from types import SimpleNamespace

import discord
from discord.ext import commands

from utils import command_scope, features

ALL_OFF = {feature.id: False for feature in features.FEATURES if feature.toggleable}
ALL_ON = {feature.id: True for feature in features.FEATURES if feature.toggleable}


class _GatedBot:
    """A bot whose module state is a plain mapping — no database, no cache."""

    def __init__(self, states):
        self.states = dict(states)

    def is_module_enabled(self, _guild_id, module_id):
        return bool(self.states.get(module_id, True))


class _Response:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _Interaction:
    """The attributes a cog's module check reads, and nothing more.

    ``root_parent`` is set the way Discord presents a subcommand —
    ``/defaultrole set`` arrives as ``"set"`` inside ``defaultrole`` — because
    ``cogs/server_control.py`` reads it to find the name that identifies the
    command, and a stub without it would make that lookup silently miss.
    """

    def __init__(self, name, root=None):
        self.guild_id = 1
        self.guild = SimpleNamespace(id=1)
        parent = SimpleNamespace(name=root) if root and root != name else None
        self.command = SimpleNamespace(name=name, root_parent=parent)
        self.response = _Response()


class TheRuleItself(unittest.TestCase):
    """The pure rule: a command's modules, and what they leave available."""

    def test_a_command_with_no_gate_is_core(self):
        self.assertEqual(command_scope.gate_of("help"), ())
        self.assertEqual(command_scope.gate_of("not-a-command"), ())

    def test_a_gated_command_names_its_modules(self):
        self.assertEqual(command_scope.gate_of("market"), ("market",))

    def test_a_shared_gate_needs_every_module_it_names(self):
        """/slots pays out of the economy and lives in the games cog."""
        self.assertEqual(sorted(command_scope.gate_of("slots")), ["economy", "games"])
        self.assertFalse(command_scope.is_available("slots", {"economy": True, "games": False}))
        self.assertFalse(command_scope.is_available("slots", {"economy": False, "games": True}))
        self.assertTrue(command_scope.is_available("slots", {"economy": True, "games": True}))

    def test_a_core_command_is_available_however_the_modules_are_set(self):
        self.assertTrue(command_scope.is_available("help", ALL_OFF))
        self.assertTrue(command_scope.is_available("dbstatus", ALL_OFF))

    def test_no_modules_off_means_everything_is_available(self):
        names = ["help", "market", "slots", "rank"]
        self.assertEqual(command_scope.hidden_names(names, ALL_ON), [])

    def test_every_module_off_hides_everything_that_is_gated(self):
        names = ["help", "market", "slots", "rank"]
        self.assertEqual(
            sorted(command_scope.hidden_names(names, ALL_OFF)),
            ["market", "rank", "slots"],
        )

    def test_a_name_the_registry_does_not_know_is_never_hidden(self):
        """Failing open: a typo upstream can leave a command visible, not delete it."""
        self.assertEqual(command_scope.hidden_names(["who-knows"], ALL_OFF), [])
        self.assertEqual(command_scope.unknown(["who-knows", "help"]), ["who-knows"])

    def test_a_missing_state_falls_back_to_the_features_own_default(self):
        """A server with no saved rows must get the same answer as its commands do."""
        self.assertEqual(
            command_scope.is_available("market", {}),
            features.default_enabled("market"),
        )

    def test_the_summary_counts_what_the_rule_hides(self):
        summary = command_scope.summarise(["help", "market", "rank"], {"market": False})
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["available"], 2)
        self.assertEqual(summary["hidden"], 1)
        self.assertEqual(summary["hidden_names"], ["market"])
        self.assertFalse(summary["modules"]["market"]["enabled"])
        self.assertTrue(summary["modules"]["leveling"]["enabled"])

    def test_the_summary_names_the_commands_each_module_owns(self):
        summary = command_scope.summarise(["help", "market", "rank"], ALL_ON)
        self.assertEqual(summary["modules"]["market"]["commands"], ["market"])
        self.assertEqual(summary["modules"]["leveling"]["commands"], ["rank"])


class TheRuleCoversTheRealTree(unittest.IsolatedAsyncioTestCase):
    """A command nobody gated and nobody owns is a command hiding cannot reach."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_every_command_is_either_gated_or_declared_core(self):
        unaccounted = [
            command.name
            for command in self.bot.tree.get_commands()
            if command.name not in features.MODULE_COMMAND_MAP
            and command.name not in features.ALWAYS_ALLOWED_COMMANDS
        ]
        self.assertEqual(
            unaccounted,
            [],
            "commands that are neither gated by a feature nor declared always-allowed",
        )

    def test_no_gate_outlives_its_command(self):
        names = {command.name for command in self.bot.tree.get_commands()}
        self.assertEqual(
            sorted(set(features.MODULE_COMMAND_MAP) - names),
            [],
            "a gate that matches no command in the tree",
        )

    @staticmethod
    def _targets(command):
        """The command objects a root name stands for: itself, or its children.

        A group carries no binding of its own — ``/market board`` is the bound
        command — so a gate on a group is a gate on each of its subcommands.
        """
        return list(getattr(command, "commands", None) or [command])

    async def _refusal(self, root, states):
        """``(refused, what was answered)`` — ``None`` when the cog has no gate.

        Only an **async** check is a module gate: discord.py's base
        ``Cog.interaction_check`` is a synchronous method returning True, so a
        cog that never overrode it is not evidence of anything either way.
        """
        verdicts = []
        texts: list[str] = []
        for target in self._targets(root):
            cog = getattr(target, "binding", None)
            check = getattr(cog, "interaction_check", None)
            if check is None or not inspect.iscoroutinefunction(check):
                continue
            cog.bot = _GatedBot(states)
            interaction = _Interaction(target.name, root.name)
            verdicts.append(await check(interaction))
            texts.extend(
                str(message.get("content") or "")
                for message in interaction.response.messages
            )
        if not verdicts:
            return None, []
        return (not all(verdicts)), texts

    @staticmethod
    def _gates_in_source(root) -> bool:
        """The bot's other way of enforcing a module: inside the callback.

        ``/activity``'s subcommands each check the tracker module themselves
        rather than through their cog, which is why this reads the source and not
        only the cog object. A cog-level check is still the common case.
        """
        modules = command_scope.gate_of(root.name)
        if not modules:
            return False
        for target in TheRuleCoversTheRealTree._targets(root):
            try:
                source = inspect.getsource(target.callback)
            except (OSError, TypeError):
                continue
            if "is_module_enabled" in source and any(
                f'"{module}"' in source for module in modules
            ):
                return True
        return False

    async def test_every_command_a_server_loses_is_one_the_bot_would_refuse(self):
        """The invariant, checked against the real cogs rather than the registry.

        Every module off, then ask each command's own cog. The refused set and
        the hidden set have to be the same set — in both directions. A command
        the registry calls gated that **nothing** enforces would be hidden while
        still working, which is the one outcome hiding must never produce.
        """
        hidden: list[str] = []
        refused: list[str] = []
        unverified: list[str] = []
        checked = 0

        for command in self.bot.tree.get_commands():
            is_hidden = not command_scope.is_available(command.name, ALL_OFF)
            if is_hidden:
                hidden.append(command.name)
            verdict, _texts = await self._refusal(command, ALL_OFF)
            if verdict is not None:
                checked += 1
            if verdict is True:
                refused.append(command.name)
            elif is_hidden and verdict is None and not self._gates_in_source(command):
                unverified.append(command.name)

        self.assertGreater(checked, 50, "the cog checks were not actually exercised")
        self.assertEqual(
            unverified,
            [],
            "commands the rule would hide that no module gate actually enforces",
        )
        self.assertEqual(
            sorted(refused),
            sorted(hidden),
            "a command hidden from a server's picker that the bot would have run, "
            "or one it would refuse and still offers",
        )

    async def test_every_refusal_names_the_module_that_is_off(self):
        """The refusal is the *module* gate, not some other gate in the way.

        The stub satisfies nothing else on purpose — no roles, no permissions,
        no market role — so a check that turned a command down for another reason
        would turn it down here too and say so, with an answer that never mentions
        the module. Every refusal has to be the one hiding is standing in for.
        """
        for command in self.bot.tree.get_commands():
            if command_scope.is_available(command.name, ALL_OFF):
                continue
            verdict, texts = await self._refusal(command, ALL_OFF)
            if verdict is None:
                continue  # /activity checks the module inside its callbacks
            self.assertTrue(verdict, f"/{command.name} was not refused with its modules off")
            self.assertTrue(
                any("turned off" in text for text in texts),
                f"/{command.name} was refused without the module gate saying why: {texts}",
            )


if __name__ == "__main__":
    unittest.main()
