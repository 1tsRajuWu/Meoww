"""Every command has a declared audience, and the declaration is true.

The permission model is only worth having if it cannot silently drift, so these
tests load the real cogs and check the real command objects:

* every command in the tree appears in ``utils/permissions.COMMAND_ACCESS``, and
  every entry still matches a command (a retired command must not leave a rule
  behind);
* each command's **own checks are actually run**, against a fake interaction
  built with only the declared permission — the declared audience must pass, and
  no permission at all must fail. That is the real ``has_permissions`` predicate
  from discord.py, not a re-implementation of it;
* an every-member command must have no permission check, so nobody is quietly
  locked out of the fun commands;
* the body-checked audiences (server owner, quote submitter, the market's role)
  are pinned by the guard the callback calls, because Discord has no permission
  bit for them;
* the picker visibility stamped before publishing matches the table, exactly and
  idempotently;
* the role-grant guard refuses each escalation path.
"""
import inspect
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from utils import features, permissions

ROOT = Path(__file__).resolve().parent.parent


def _bot_py_commands() -> set[str]:
    """Commands registered in ``bot.py`` rather than a cog.

    They are in the real tree but not in a cog-loading test, so the table has to
    cover them from the source instead of pretending they do not exist. Nothing
    lives there today — ``/dashboard`` was the last one, and it is now the ⚡
    button on the ``/help`` panel — so this guard is for the next one.
    """
    source = (ROOT / "bot.py").read_text()
    return set(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', source))

#: Commands whose guard lives in the callback because Discord has no permission
#: bit for it, and the guard each one has to call.
BODY_GUARDS = {
    "eco-admin set": "check_is_owner",
    "eco-admin reset": "check_is_owner",
    "resetlevels": "owner",
    "customquote delete": "creator_id",
}

#: Commands that hand out roles, and must therefore run the escalation guard.
ROLE_GRANT_COMMANDS = ("addrole", "removerole", "levelroles add")


class _FakeInteraction:
    """The two attributes a permission check reads, and nothing else."""

    def __init__(self, member_permissions: discord.Permissions):
        self.guild = SimpleNamespace(id=1, default_role=None)
        self.permissions = member_permissions


def _gate(command, member_permissions: discord.Permissions) -> tuple[bool, int]:
    """Run a command's own checks, as discord.py would before the callback.

    Returns ``(allowed, sync_checks)`` — async checks (cooldowns) are closed
    rather than awaited, since they are not permission gates and this test does
    not need their verdict.
    """
    interaction = _FakeInteraction(member_permissions)
    allowed = True
    sync_checks = 0
    for check in command.checks:
        try:
            result = check(interaction)
        except app_commands.CheckFailure:
            # discord.py's permission checks *raise* rather than return False;
            # that exception is exactly the refusal the member would see.
            sync_checks += 1
            allowed = False
            continue
        if inspect.isawaitable(result):
            result.close()
            continue
        sync_checks += 1
        allowed = allowed and bool(result)
    return allowed, sync_checks


class TestTheTableMatchesTheTree(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")
        self.commands = list(permissions.walk(self.bot.tree))

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_every_command_declares_an_audience(self):
        self.assertEqual(
            permissions.undeclared(self.bot.tree),
            [],
            "commands with no declared audience in utils/permissions.py",
        )

    def test_no_declaration_outlives_its_command(self):
        stale = [
            path
            for path in permissions.unknown_declarations(self.bot.tree)
            if path not in _bot_py_commands()
        ]
        self.assertEqual(stale, [], "declared audiences that match no command")

    def test_bot_py_commands_declare_an_audience_too(self):
        for name in _bot_py_commands():
            self.assertIn(name, permissions.COMMAND_ACCESS, f"/{name} has no audience")

    def test_the_tree_is_what_we_think_it_is(self):
        # A sanity floor: if the walker silently returned nothing, every test
        # below would pass for the wrong reason.
        self.assertGreater(len(self.commands), 60)

    def test_an_every_member_command_has_no_permission_check(self):
        for path, command in self.commands:
            if permissions.COMMAND_ACCESS[path] != permissions.EVERYONE:
                continue
            allowed, sync_checks = _gate(command, discord.Permissions.none())
            self.assertTrue(allowed, f"/{path} refuses a member with no permissions")
            self.assertEqual(sync_checks, 0, f"/{path} gained a permission check")

    def test_every_permission_gated_command_enforces_exactly_what_it_declares(self):
        for path, command in self.commands:
            required = permissions.PERMISSION_CHECK.get(
                permissions.COMMAND_ACCESS[path]
            )
            if required is None:
                continue
            granted, sync_checks = _gate(command, required)
            self.assertGreater(sync_checks, 0, f"/{path} has no check for its audience")
            self.assertTrue(granted, f"/{path} refuses the audience it declares")
            refused, _ = _gate(command, discord.Permissions.none())
            self.assertFalse(refused, f"/{path} runs for a member with no permissions")

    def test_a_body_checked_command_calls_its_guard(self):
        by_path = dict(self.commands)
        for path, marker in BODY_GUARDS.items():
            command = by_path[path]
            source = inspect.getsource(command.callback)
            self.assertIn(marker, source, f"/{path} lost its {marker!r} guard")

    def test_the_declared_non_permission_audiences_are_body_checked(self):
        for path, command in self.commands:
            requirement = permissions.COMMAND_ACCESS[path]
            if requirement not in permissions.SOURCE_MARKER:
                continue
            if requirement == permissions.MARKET:
                # The market's gate is one hook for the whole group. Compared by
                # source rather than by class identity: the cog-test loads the
                # module a second time, so ``isinstance`` is not reliable here.
                binding = command.binding
                self.assertIsNotNone(binding, path)
                self.assertIn(
                    "access_problem",
                    inspect.getsource(type(binding).interaction_check),
                    "the market's role gate is gone",
                )
                continue
            marker = permissions.SOURCE_MARKER[requirement]
            self.assertIn(
                marker, inspect.getsource(command.callback), f"/{path} lost its guard"
            )

    def test_role_granting_commands_run_the_escalation_guard(self):
        by_path = dict(self.commands)
        for name in ROLE_GRANT_COMMANDS:
            self.assertIn(
                "role_grant_problem",
                inspect.getsource(by_path[name].callback),
                f"/{name} can hand out a role without checking whether it should",
            )

    def test_the_stamp_matches_the_table_and_is_idempotent(self):
        before = {
            path: command.default_permissions for path, command in self.commands
        }
        changed = set(permissions.apply_declared_permissions(self.bot.tree))
        expected = set()
        for path, command in self.commands:
            wanted = permissions.PICKER_VISIBILITY[permissions.COMMAND_ACCESS[path]]
            self.assertEqual(command.default_permissions, wanted, path)
            if before[path] != wanted:
                expected.add(path)
        self.assertEqual(changed, expected)
        # Running it again must be a no-op, or every boot would re-upload.
        self.assertEqual(permissions.apply_declared_permissions(self.bot.tree), [])


class _Actor:
    """A member as the role guard reads them."""

    def __init__(self, *, administrator=False, manage_roles=False, top_name="Head Mod"):
        self.guild_permissions = discord.Permissions(
            administrator=administrator, manage_roles=manage_roles
        )
        self.top_role = _Role(name=top_name, position=5)


class _Role:
    def __init__(
        self,
        *,
        name="Role",
        position=1,
        role_id=1,
        default=False,
        managed=False,
        permissions=None,
    ):
        self.name = name
        self.position = position
        self.id = role_id
        self.managed = managed
        self.permissions = permissions or discord.Permissions.none()
        self._default = default

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __gt__(self, other):
        return self.position > other.position


class _Guild:
    def __init__(self, default_role):
        self.default_role = default_role


class TestTheRoleGrantGuard(unittest.TestCase):
    """Each refusal is an escalation path that used to be open."""

    def setUp(self):
        self.everyone = _Role(name="@everyone", role_id=10, default=True, position=0)
        self.guild = _Guild(self.everyone)

    def test_a_plain_role_below_you_is_fine(self):
        actor = _Actor(manage_roles=True)
        role = _Role(name="Member", position=1)
        self.assertIsNone(permissions.role_grant_problem(role, actor, self.guild))

    def test_everyone_cannot_be_handed_out(self):
        actor = _Actor(manage_roles=True)
        self.assertIn(
            "@everyone", permissions.role_grant_problem(self.everyone, actor, self.guild)
        )

    def test_a_managed_role_cannot_be_handed_out(self):
        actor = _Actor(manage_roles=True)
        role = _Role(name="Nitro Booster", position=2, managed=True)
        self.assertIn("managed", permissions.role_grant_problem(role, actor, self.guild))

    def test_the_escalation_this_guard_exists_for(self):
        """A moderator could grant themselves Administrator, below their own role."""
        actor = _Actor(manage_roles=True, top_name="Moderator")
        admin_only = _Role(
            name="Admin", position=3, permissions=discord.Permissions(administrator=True)
        )
        problem = permissions.role_grant_problem(admin_only, actor, self.guild)
        self.assertIsNotNone(problem)
        self.assertIn("Administrator", problem)

    def test_a_role_at_or_above_your_own_is_refused(self):
        actor = _Actor(manage_roles=True, top_name="Moderator")
        peer = _Role(name="Moderator", position=5)
        self.assertIn("highest role", permissions.role_grant_problem(peer, actor, self.guild))

    def test_an_administrator_may_grant_anything_assignable(self):
        actor = _Actor(administrator=True, top_name="Owner")
        role = _Role(
            name="Admin", position=9, permissions=discord.Permissions(administrator=True)
        )
        self.assertIsNone(permissions.role_grant_problem(role, actor, self.guild))

    def test_ordinary_moderation_powers_are_not_escalation(self):
        """A moderator promoting another moderator to Staff must still work."""
        actor = _Actor(manage_roles=True, top_name="Head Mod")
        staff = _Role(
            name="Staff",
            position=3,
            permissions=discord.Permissions(manage_messages=True, moderate_members=True),
        )
        self.assertIsNone(permissions.role_grant_problem(staff, actor, self.guild))

    def test_a_role_that_no_longer_exists(self):
        actor = _Actor(manage_roles=True)
        self.assertIn("no longer exists", permissions.role_grant_problem(None, actor, self.guild))


if __name__ == "__main__":
    unittest.main()
