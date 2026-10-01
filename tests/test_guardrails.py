"""The guardrails around the management tier, held to the commands that use them.

A bot that can delete a server's channels is a different risk from one that
publishes a leaderboard, and the whole of that difference is in three properties —
each of which is cheap now and impossible to retrofit once a server has lost a
channel to a command nobody confirmed:

* **Size is checked before the door opens.** Below its ceiling an action runs;
  above it, nothing happens until the person who typed the command presses a
  button, and a press from anybody else is not a press.
* **Every write is written down.** One door per kind of change opens the case and
  posts the server's own moderation log entry; a command that writes to Discord
  itself is a state change with no record, and that is the one thing the tier is
  not allowed to contain.
* **A refusal is a fact too.** A role above the bot's own, a managed role, a name
  Discord will not take — each is answered *and* logged, because the log is how an
  administrator finds out that the thing they asked for did not happen.

The last class here is the one that ages well: a pinned inventory of every file
that still writes to a Discord object directly, so a **new** one cannot appear
without somebody having to write down why.
"""
import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.support import TempDatabaseMixin
from utils import confirmations, db, features, moderation_actions, structure

ROOT = Path(__file__).resolve().parent.parent
COGS = ROOT / "cogs"

GUILD_ID = 777501
ACTOR_ID = 900001

#: The methods that change something a person would notice. Attribute names rather
#: than an allow-list of receivers, because the point is to catch the call whatever
#: it was called on.
WRITES = frozenset({
    "delete", "create_text_channel", "create_voice_channel", "create_role",
    "create_category", "remove_roles", "ban", "kick", "timeout", "edit", "purge",
    "set_permissions", "clone",
})

#: Objects that may be written to without going through a door: the doors
#: themselves, the database, and the library's own classes. Everything else is a
#: direct change to somebody's server.
DOORS = frozenset({"structure", "moderation_actions", "db", "discord", "discord_utils"})

#: Every file in ``cogs/`` that still writes to a Discord object itself, with the
#: reason it is allowed to. Pinned rather than derived: this list is the record of
#: a decision, and a *new* entry means somebody added a write that skips the door.
#: The two entries marked **pre-existing** are named as gaps rather than blessed —
#: the ticket lifecycle and the lockdown permission edits are structure changes
#: that predate this door, and closing them is its own pass, not this one.
DIRECT_WRITES = {
    "alonetime.py": [
        "channel.edit()",        # the cap follows the holder's list, and the change opens a case
    ],
    "automod.py": [
        "message.delete()",      # a message, not server state; automod records its own actions
        "author.timeout()",      # recorded through utils.moderation_actions
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
        "channel.set_permissions()",  # pre-existing: /lockdown, stored via save_lockdown
        "channel.edit()",             # pre-existing: /slowmode
        "member.remove_roles()",      # pre-existing: /defaultrole clear
    ],
    "social.py": [
        "msg.edit()",            # the bot editing its own embed
    ],
    "starboard.py": [
        "star_msg.delete()",     # removing a starboard copy, not a channel
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


class TheDoorIsTheOnlyWriter(unittest.TestCase):
    """The command file for the management tier writes nothing itself."""

    def test_the_structure_cog_performs_no_discord_write(self):
        """Every write goes through ``utils.structure``, which is what opens the case.

        This is the mechanism behind the brief's non-goal — *no shipped command
        without an audit-log entry if it modifies server state* — expressed as a
        property of the source rather than as a promise in a docstring: the
        command layer has no way to delete a channel except by asking the door.
        """
        self.assertEqual(
            direct_writes(COGS / "structure.py"),
            [],
            "the structure cog writes to Discord directly, so those writes open no case",
        )

    def test_the_door_is_where_that_file_writes(self):
        """The other half: it has to actually call the door, or the law above is vacuous."""
        source = (COGS / "structure.py").read_text(encoding="utf-8")
        for function in (
            "create_channel", "delete_channels", "create_role", "delete_role", "purge_role",
        ):
            self.assertIn(f"structure.{function}(", source, f"the cog never calls {function}()")

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


class TheSizeIsCheckedFirst(unittest.TestCase):
    """Ceilings, and the confirmation that a ceiling exists to keep cheap."""

    def test_the_rule_is_a_function_and_not_a_callback(self):
        self.assertTrue(confirmations.needed(4, 3))
        self.assertFalse(confirmations.needed(3, 3), "at the ceiling is below the ceiling")
        self.assertFalse(confirmations.needed(0, 1))

    def test_every_destructive_action_has_a_ceiling(self):
        """A destructive action with no ceiling is one that would run unconfirmed."""
        missing = sorted(set(structure.DESTRUCTIVE) - set(confirmations.CEILINGS))
        self.assertEqual(missing, [], "these destroy something and have no ceiling")
        # And the verb table is shared, so a case reads the same wherever it opened.
        for action in structure.DESTRUCTIVE:
            self.assertIn(action, moderation_actions.ACTIONS)

    def test_a_one_item_action_is_never_prompted(self):
        """The prompt that matters is only worth what the boring ones have not trained."""
        interaction = _FakeInteraction()
        allowed = asyncio.run(
            confirmations.guard(interaction, action="channel_delete", count=1, subject="channel")
        )
        self.assertTrue(allowed)
        self.assertEqual(interaction.response.messages, [], "a single channel was prompted")

    def test_a_mass_action_waits_for_the_asker_and_never_proceeds_on_its_own(self):
        interaction = _FakeInteraction()
        original = confirmations.Confirmation.wait

        async def never_answered(self):
            return None

        confirmations.Confirmation.wait = never_answered
        try:
            allowed = asyncio.run(
                confirmations.guard(
                    interaction, action="channel_archive", count=40, subject="channels in Chat"
                )
            )
        finally:
            confirmations.Confirmation.wait = original
        self.assertFalse(allowed, "a mass deletion went ahead without an answer")
        self.assertEqual(len(interaction.response.messages), 1)
        prompt = interaction.response.messages[0]
        self.assertTrue(prompt["ephemeral"], "the prompt has to be the asker's alone")
        view = prompt["view"]
        self.assertIsInstance(view, confirmations.Confirmation)
        self.assertIsNone(view.finished, "a timeout is not a decision")
        # What it says: the number, the ceiling, and that there is no undo.
        self.assertIn("40", prompt["content"])
        self.assertIn(str(confirmations.CEILINGS["channel_archive"]), prompt["content"])
        self.assertIn("cannot be undone", prompt["content"])

    def test_only_the_asker_can_answer_it(self):
        """A confirmation a bystander can give is decoration."""
        view = confirmations.Confirmation(ACTOR_ID, subject="channels", count=12)
        bystander = _FakeInteraction(user_id=ACTOR_ID + 1)
        asyncio.run(view._decide(bystander, True))
        self.assertEqual(view.refused_presses, 1)
        self.assertIsNone(view.finished, "a bystander's press counted")
        self.assertIn("not yours", bystander.response.messages[-1]["content"])

        asker = _FakeInteraction(user_id=ACTOR_ID)
        asyncio.run(view._decide(asker, True))
        self.assertTrue(view.finished)

    def test_the_prompt_offers_both_answers(self):
        view = confirmations.Confirmation(ACTOR_ID, subject="roles", count=3)
        self.assertEqual(
            sorted(child.label for child in view.children),
            ["Cancel", "Yes, do it"],
        )


class TheCeilingIsThisServers(TempDatabaseMixin, unittest.TestCase):
    """A ceiling is a stored setting per action per server, and nobody else's."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    def test_the_default_comes_from_the_table(self):
        self.assertEqual(
            confirmations.ceiling_for(GUILD_ID, "channel_archive"),
            confirmations.CEILINGS["channel_archive"],
        )

    def test_a_server_can_raise_or_lower_it(self):
        db.write_setting_sync(GUILD_ID, "confirmation_ceiling.channel_archive", 40)
        self.assertEqual(confirmations.ceiling_for(GUILD_ID, "channel_archive"), 40)
        db.write_setting_sync(GUILD_ID, "confirmation_ceiling.channel_archive", 0)
        self.assertEqual(confirmations.ceiling_for(GUILD_ID, "channel_archive"), 0)
        # And it is that server's alone.
        self.assertEqual(
            confirmations.ceiling_for(GUILD_ID + 1, "channel_archive"),
            confirmations.CEILINGS["channel_archive"],
        )

    def test_a_corrupt_setting_never_becomes_a_bigger_limit(self):
        """The only direction of corruption that matters here."""
        for junk in ("banana", None, -3, confirmations.MAX_CEILING + 1):
            db.write_setting_sync(GUILD_ID, "confirmation_ceiling.role_purge", junk)
            self.assertEqual(
                confirmations.ceiling_for(GUILD_ID, "role_purge"),
                confirmations.CEILINGS["role_purge"],
                f"{junk!r} was accepted as a ceiling",
            )


class TheRecordIsWritten(TempDatabaseMixin, unittest.TestCase):
    """Every door opens a case; a refusal leaves a card and no case."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        self.guild = _FakeGuild()
        self.actor = moderation_actions.Actor(ACTOR_ID, "an administrator", member=None)

    def tearDown(self):
        self.release_temp_database()

    def _cases(self) -> list[tuple]:
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT case_type, moderator_id, reason FROM mod_cases WHERE guild_id = ?",
                (GUILD_ID,),
            ).fetchall()
        finally:
            conn.close()

    def test_a_channel_that_was_deleted_becomes_a_case(self):
        result = asyncio.run(
            structure.delete_channels(self.guild, self.actor, [self.guild.channels[0]], reason="tidy up")
        )
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self._cases(), [("Channel deleted", ACTOR_ID, "tidy up")])
        self.assertIsNotNone(result.case_id)

    def test_a_role_that_was_created_becomes_a_case(self):
        result = asyncio.run(
            structure.create_role(self.guild, self.actor, "Regulars", reason="for the regulars")
        )
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(self._cases(), [("Role created", ACTOR_ID, "for the regulars")])

    def test_a_refusal_is_answered_and_opens_no_case(self):
        """``@everyone`` is not a role, and saying so is not a case."""
        everyone = _FakeRole(name="@everyone", is_default=True)
        result = asyncio.run(structure.delete_role(self.guild, self.actor, everyone, reason=""))
        self.assertFalse(result.ok)
        self.assertIn("@everyone", result.detail)
        self.assertEqual(result.refused_by, "guard")
        self.assertEqual(self._cases(), [], "a refusal was recorded as something that happened")

    def test_a_role_above_the_bot_is_refused_with_the_reason(self):
        high = _FakeRole(name="Admin", position=90)
        refusal = structure.deletable_role(self.guild, high)
        self.assertIn("top role", refusal)
        # And the door agrees with the answer the command shows before it opens.
        result = asyncio.run(structure.delete_role(self.guild, self.actor, high, reason=""))
        self.assertFalse(result.ok)
        self.assertEqual(self._cases(), [])

    def test_a_managed_role_is_refused(self):
        managed = _FakeRole(name="Server Booster", managed=True)
        self.assertIn("integration", structure.deletable_role(self.guild, managed))

    def test_purging_a_role_counts_people_and_not_roles(self):
        """The blast radius that is invisible in its own command line."""
        holders = [_FakeMember(index) for index in range(7)]
        role = _FakeRole(name="Muted", members=holders)
        result = asyncio.run(structure.purge_role(self.guild, self.actor, role, reason="reset"))
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.count, 7)
        self.assertEqual([member.removed for member in holders], [role] * 7)
        self.assertEqual(self._cases(), [("Role removed from members", ACTOR_ID, "reset")])

    def test_an_empty_purge_says_so_instead_of_opening_a_case(self):
        result = asyncio.run(
            structure.purge_role(self.guild, self.actor, _FakeRole(name="Idle", members=[]), reason="")
        )
        self.assertFalse(result.ok)
        self.assertIn("Nobody has", result.detail)
        self.assertEqual(self._cases(), [])

    def test_a_name_discord_would_refuse_never_reaches_discord(self):
        result = asyncio.run(structure.create_channel(self.guild, self.actor, "   ", reason=""))
        self.assertFalse(result.ok)
        self.assertEqual(result.refused_by, "input")
        self.assertEqual(self.guild.created, [])
        self.assertEqual(self._cases(), [])


class TheSurfaceIsReal(unittest.TestCase):
    """The registry, the tree and the picker have to agree about all of this."""

    def test_the_feature_is_declared_with_its_cog_and_its_gate(self):
        feature = features.get("structure")
        self.assertIsNotNone(feature)
        self.assertEqual(feature.gate_commands, ("structure",))
        self.assertEqual(feature.cog, "cogs.structure")
        self.assertTrue(feature.toggleable, "a server has to be able to switch this off")

    def test_every_declared_command_is_in_its_description(self):
        """The dashboard card is where a server learns what it turned on."""
        listed = " ".join(features.get("structure").commands)
        for name in (
            "/structure channel-create", "/structure channel-delete",
            "/structure channel-archive", "/structure role-create",
            "/structure role-delete", "/structure role-purge", "/structure limits",
        ):
            self.assertIn(name, listed)

    def test_the_limits_command_is_the_administrators(self):
        from utils import permissions

        self.assertEqual(permissions.COMMAND_ACCESS.get("structure limits"), permissions.ADMIN)


class _FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return bool(self.messages)

    async def send_message(self, content=None, embed=None, view=None, ephemeral=False):
        self.messages.append(
            {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}
        )

    async def edit_message(self, content=None, view=None, embed=None):
        self.messages.append({"content": content, "edited": True, "view": view})


class _FakeInteraction:
    def __init__(self, *, user_id: int = ACTOR_ID, guild_id: int = GUILD_ID):
        self.user = SimpleNamespace(id=user_id, name="an administrator")
        self.guild_id = guild_id
        self.response = _FakeResponse()


class _FakeRole:
    """A role with the four properties the guard reads, and a position to compare."""

    _next_id = 500

    def __init__(self, *, name="Role", position=1, members=(), managed=False, is_default=False):
        _FakeRole._next_id += 1
        self.id = _FakeRole._next_id
        self.name = name
        self.position = position
        self.members = list(members)
        self.managed = managed
        self._default = is_default

    def is_default(self) -> bool:
        return self._default

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)

    def __lt__(self, other):
        return self.position < getattr(other, "position", 0)

    async def delete(self, reason=None):
        return None


class _FakeMember:
    def __init__(self, index: int = 0):
        self.id = 600000 + index
        self.name = f"member-{index}"
        self.display_name = self.name
        self.removed: _FakeRole | None = None

    async def remove_roles(self, role, reason=None):
        self.removed = role


class _FakeChannel:
    def __init__(self, name: str, guild):
        self.id = 700000 + len(getattr(guild, "channels", []))
        self.name = name
        self.guild = guild

    async def delete(self, reason=None):
        return None

    def permissions_for(self, _member):
        return SimpleNamespace(manage_channels=True)


class _FakeGuild:
    """Only what the door reads: an id, a me, a default role and the two writes."""

    def __init__(self):
        self.id = GUILD_ID
        self.name = "A Server"
        self.default_role = _FakeRole(name="@everyone", position=0, is_default=True)
        self.me = SimpleNamespace(id=999, top_role=_FakeRole(name="Bot", position=50))
        self.channels = [_FakeChannel("general", self), _FakeChannel("chat", self)]
        self.created: list = []

    async def create_text_channel(self, name, **kwargs):
        self.created.append(name)
        return _FakeChannel(name, self)

    async def create_role(self, name, colour=None, reason=None):
        self.created.append(name)
        return _FakeRole(name=name)

    def get_channel(self, _channel_id):
        return None

    def get_member(self, _member_id):
        return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
