"""The systems this bot grew up on: kept whole, switched off, one flag per server.

Moving Radio and Anonymous Q&A out of the active set is the one part of the
reorganisation that can break a server which did nothing wrong, so what is held
here is the set of properties that stop it being a *silent* break — and each of
them is a way this could have gone quietly wrong:

* **Nothing was deleted.** Both systems are still registered, still loaded as
  cogs, still answering to their own root names, and their files are still in the
  tree. A server with the flag on has exactly what it had yesterday.
* **One switch, off by default, per server.** Not a refusal at run time: the
  commands leave that server's picker (``utils/command_scope``), because Discord
  cannot hide a globally-registered command in one server.
* **A server that was using either system keeps it.** The state is carried on
  evidence — a held seat, a station played, a saved preference, a question asked,
  or a switch somebody deliberately turned on — rather than asked for, and it is
  carried once so a server that turns it back off stays off.
* **The switch is reachable from Discord**, by the person who can make the
  decision, and both systems name it when they refuse.

``tests/test_modules.py`` holds the module cache and ``tests/test_commands.py``
holds the command tree; this file holds the seam between them.
"""
import asyncio
import re
import unittest
from pathlib import Path

import aiosqlite
import discord
from discord.ext import commands

from tests.support import TempDatabaseMixin
from utils import command_scope, commands_sync, db, features, modules, qna

ROOT = Path(__file__).resolve().parent.parent

#: Two servers that never touch each other: the per-server half of every test
#: below is "did this one change and that one not".
GUILD = 777401
OTHER = 777402

#: The switch's own name, and the two systems it keeps. Spelled once here so a
#: rename in one place fails loudly rather than half of these tests reading the
#: old word.
FLAG = "legacy"
KEPT = ("radio", "qna")


class TheOneSwitch(unittest.TestCase):
    """The registry's half: one feature, two roots, off for anybody who asks."""

    def test_the_two_systems_are_one_feature(self):
        """One flag, because they left the active set for the same reason."""
        for old in KEPT:
            self.assertIsNone(
                features.get(old),
                f"'{old}' is half of the {FLAG} feature, not a feature of its own",
            )
        feature = features.get(FLAG)
        self.assertIsNotNone(feature)
        self.assertEqual(sorted(feature.gate_commands), sorted(KEPT))

    def test_off_by_default_is_the_declared_state(self):
        feature = features.get(FLAG)
        self.assertTrue(feature.toggleable, "a server has to be able to switch it")
        self.assertFalse(feature.enabled_by_default)
        self.assertIn(FLAG, features.DEFAULT_DISABLED_MODULES)
        self.assertFalse(features.default_enabled(FLAG))
        # The whole point of a declared default: a server with no saved row gets
        # the same answer from the picker as it gets from the command's own gate.
        self.assertFalse(command_scope.is_available("radio", {}))
        self.assertFalse(command_scope.is_available("qna", {}))
        self.assertTrue(command_scope.is_available("radio", {FLAG: True}))

    def test_the_flag_is_the_only_thing_that_hides_them(self):
        """Both roots gate on the flag — and on nothing else."""
        for root in KEPT:
            self.assertEqual(
                features.MODULE_COMMAND_MAP.get(root),
                (FLAG,),
                f"/{root} must be gated by '{FLAG}' alone",
            )

    def test_neither_system_is_retired_by_name(self):
        """A name leaves the picker only by being on ``RETIRED_COMMANDS``.

        Nothing here may put ``radio`` or ``qna`` on that list: retiring a name is
        how a command is *deleted* from every scope, and these two are kept — a
        server that switches the flag on has to get them back without a redeploy.
        """
        for root in KEPT:
            self.assertNotIn(root, commands_sync.RETIRED_COMMANDS)

    def test_both_cogs_are_still_loaded(self):
        """'Switched off' and 'unloaded' are different things, and this is which."""
        paths = set(features.cog_paths())
        for cog in ("cogs.radio", "cogs.qna"):
            self.assertIn(cog, paths)

    def test_the_files_are_still_in_the_tree(self):
        """The bluntest reading of 'nothing is deleted'."""
        for relative in (
            "cogs/radio.py",
            "cogs/qna.py",
            "utils/radio.py",
            "utils/radio_catalogue.py",
            "utils/radio_stream.py",
            "utils/qna.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), f"{relative} is gone")

    def test_the_registry_still_lists_every_command_it_keeps(self):
        """The dashboard card is the only place a server learns what it turned on."""
        feature = features.get(FLAG)
        listed = " ".join(feature.commands)
        for name in ("/radio play", "/radio stop", "/radio doctor", "/qna ask", "/qna inbox", "/qna reply"):
            self.assertIn(name, listed)

    def test_the_migration_and_the_registry_spell_the_flag_the_same_way(self):
        """Two files name this row — the registry and the migration that writes it.

        A migration writes a string into ``guild_modules`` and the registry decides
        whether that string means anything, so a rename in one of them would leave
        a row that quietly does nothing.
        """
        self.assertEqual(db.LEGACY_MODULE_ID, FLAG)
        self.assertIsNotNone(features.get(db.LEGACY_MODULE_ID))


class TheCommandTree(unittest.IsolatedAsyncioTestCase):
    """The tree's half: both groups are still published, whole."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        for cog in features.cog_paths():
            await self.bot.load_extension(cog)
        self.tree = {
            command.name: (
                sorted(sub.name for sub in command.commands)
                if isinstance(command, discord.app_commands.Group)
                else None
            )
            for command in self.bot.tree.get_commands()
        }

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_both_groups_publish_the_subcommands_they_had(self):
        self.assertEqual(self.tree.get("radio"), ["doctor", "play", "stop"])
        self.assertEqual(self.tree.get("qna"), ["ask", "inbox", "reply"])

    def test_the_switch_is_a_command_and_not_a_new_slot(self):
        """/bot is a group precisely so four commands cost one of Discord's hundred.

        The switch lives inside it rather than becoming ``/config``: this bot
        publishes most of that budget already, and ``test_commands.py`` fails when
        the tree stops keeping a slot free.
        """
        self.assertIn(FLAG, self.tree.get("bot") or [])
        self.assertNotIn("config", self.tree)


class TheSwitchItself(unittest.TestCase):
    """The surface's half: the person who can decide can find it, and is named."""

    def setUp(self):
        self.config = (ROOT / "cogs" / "bot_config.py").read_text(encoding="utf-8")
        self.help = (ROOT / "cogs" / "help.py").read_text(encoding="utf-8")

    def test_the_command_exists_and_is_admin_only(self):
        from utils import permissions

        self.assertIn('name="legacy"', self.config)
        self.assertEqual(permissions.COMMAND_ACCESS.get("bot legacy"), permissions.ADMIN)
        # The decorator pinned to *this* command rather than to the file: the group
        # has other members, and one of them gaining the check is not this one
        # keeping it.
        self.assertRegex(
            self.config,
            r'has_permissions\(administrator=True\)\s*\n\s*async def legacy\(',
        )

    def test_it_writes_through_the_one_module_writer(self):
        """No second write path: persistence is ``modules.set_enabled`` and nothing else."""
        self.assertIn("modules.set_enabled(guild_id, \"legacy\"", self.config)
        self.assertNotIn("INSERT INTO guild_modules", self.config)

    def test_it_republicises_the_command_list(self):
        """A switch that does not republish leaves the picker lying until the next boot."""
        self.assertIn("resync_commands", self.config)
        self.assertIn("_republish", self.config)

    def test_both_systems_name_the_switch_that_refused_them(self):
        """The message a member gets has to say which flag, and who can flip it."""
        for relative, subject in (("cogs/radio.py", "Radio"), ("cogs/qna.py", "Anonymous Q&A")):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('is_module_enabled(interaction.guild_id, "legacy")', source)
            self.assertIn(subject, source)
            self.assertIn("/bot legacy", source)

    def test_the_help_card_teaches_the_flag_rather_than_the_commands(self):
        """The card is where somebody lands after a command stops being offered."""
        self.assertIn('"legacy": {', self.help)
        self.assertIn("/bot legacy state:on", self.help)
        for retired in ('"radio": {', '"qna": {'):
            self.assertNotIn(retired, self.help)


class TheCarriedState(TempDatabaseMixin, unittest.TestCase):
    """A server that was *using* one of these keeps it, and a fresh one does not.

    The migration runs inside ``db.setup()`` behind a marker, so every test here
    deletes the marker first: that is a database which has never booted this
    version, which is exactly the one the rule was written for.
    """

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        modules.invalidate()
        self._open_the_migration()

    def tearDown(self):
        modules.invalidate()
        self.release_temp_database()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _open_the_migration(self) -> None:
        async def forget():
            async with aiosqlite.connect(db.DB_PATH) as conn:
                await conn.execute(
                    "DELETE FROM guild_settings WHERE guild_id = 0 AND key = 'legacy_module_v1'"
                )
                await conn.commit()

        asyncio.run(forget())

    def _seed(self, statements: list[tuple[str, tuple]]) -> None:
        async def work():
            async with aiosqlite.connect(db.DB_PATH) as conn:
                for sql, params in statements:
                    await conn.execute(sql, params)
                await conn.commit()

        asyncio.run(work())

    def _carry(self) -> None:
        async def work():
            async with aiosqlite.connect(db.DB_PATH) as conn:
                await db._carry_legacy_module(conn)
                await conn.commit()

        asyncio.run(work())

    def _marker(self):
        """The one row that says the carrier has run, or ``None`` when it has not."""
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT value FROM guild_settings WHERE guild_id = 0 AND key = 'legacy_module_v1'"
            ).fetchone()
        finally:
            conn.close()

    def _legacy_rows(self) -> dict[int, bool]:
        """Every ``legacy`` row there is, as ``{guild_id: enabled}``."""
        rows = db.connect().execute(
            "SELECT guild_id, enabled FROM guild_modules WHERE module_name = 'legacy'"
        ).fetchall()
        return {int(guild): bool(enabled) for guild, enabled in rows}

    def _remember_a_question(self, guild_id: int) -> None:
        """Create the inbox the way its own module does, then put a row in it."""
        self._seed([
            (qna.CREATE_TABLE, ()),
            (
                "INSERT INTO qna_questions (guild_id, guild_name, user_id, user_name, "
                "question, asked_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, "Somewhere", 42, "asker", "is this on?", 1.0, "open"),
            ),
        ])

    # ── what is carried ───────────────────────────────────────────────────────

    def test_a_server_that_played_the_radio_keeps_it(self):
        self._seed([
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    def test_a_server_that_held_a_seat_keeps_it(self):
        self._seed([
            ("INSERT INTO radio_stay (guild_id, channel_id, enabled) VALUES (?, ?, ?)",
             (GUILD, 555, 1)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    def test_a_server_that_asked_a_question_keeps_the_box(self):
        self._remember_a_question(GUILD)
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    def test_a_saved_preference_counts_as_use(self):
        """'Never touched a switch' and 'never used it' are different servers."""
        self._seed([
            ("INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
             (GUILD, "radio_ads", '"ad-free"')),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    def test_an_unrelated_switch_does_not_hide_a_server_that_used_it(self):
        """The guard reads the *old* names, and nothing else on the row matters.

        A subquery that matched any saved row at all would look identical in every
        other test here — an empty ``guild_modules`` makes it a no-op — and would
        silently skip every server that had ever touched any other switch, which
        is all of them.
        """
        self._seed([
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "economy", 1)),
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "market", 0)),
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    def test_a_server_that_deliberately_turned_one_on_keeps_it(self):
        """A stored row is a decision, so it is carried even with no evidence."""
        self._seed([
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "radio", 1)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})

    # ── who is left alone ─────────────────────────────────────────────────────

    def test_a_server_that_never_touched_either_is_left_off(self):
        """No row at all is the *new* default, which is the whole point of moving these."""
        self._seed([
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (OTHER, "economy", 1)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {})

    def test_a_server_that_switched_both_off_on_purpose_stays_off(self):
        """The one case where evidence is overruled: somebody already said no."""
        self._seed([
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "radio", 0)),
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "qna", 0)),
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {})

    def test_the_carried_state_is_that_server_and_not_the_fleet(self):
        self._seed([
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        self.assertEqual(self._legacy_rows(), {GUILD: True})
        self.assertTrue(modules.is_enabled(GUILD, FLAG))
        modules.invalidate(OTHER)
        self.assertFalse(modules.is_enabled(OTHER, FLAG))

    def test_a_carried_server_actually_gets_its_commands_back(self):
        """The row is only worth writing if the picker follows it."""
        self._seed([
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        modules.invalidate(GUILD)
        states = modules.get_states(GUILD)
        self.assertTrue(command_scope.is_available("radio", states))
        self.assertTrue(command_scope.is_available("qna", states))
        other_states = modules.get_states(OTHER)
        self.assertFalse(command_scope.is_available("radio", other_states))

    def test_running_it_twice_changes_nothing(self):
        self._seed([
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        first = self._legacy_rows()
        self._carry()
        self.assertEqual(self._legacy_rows(), first)

    def test_the_old_rows_are_left_exactly_where_they_are(self):
        """A rollback has to find its own state untouched — the rename rule."""
        self._seed([
            ("INSERT INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
             (GUILD, "radio", 1)),
        ])
        self._carry()
        conn = db.connect()
        try:
            kept = conn.execute(
                "SELECT enabled FROM guild_modules WHERE guild_id = ? AND module_name = 'radio'",
                (GUILD,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(kept, (1,))

    def test_a_second_boot_does_not_turn_it_back_on(self):
        """The marker is the difference between a migration and a policy.

        An administrator who switches the systems off after the reorganisation has
        to stay switched off, so the evidence is *not* re-read on every boot. What
        is pinned here is that *outcome* rather than the mechanism: the carrier is
        also ``INSERT OR IGNORE`` (see ``test_running_it_twice_changes_nothing``),
        so a boot that ran it again would still not overwrite a row somebody set —
        the marker is what stops the second run from existing at all, and the
        idempotence is what makes it safe if it ever did.
        """
        self._seed([
            ("INSERT INTO radio_last (guild_id, station, as_of) VALUES (?, ?, ?)",
             (GUILD, "somafm", 1.0)),
        ])
        self._carry()
        modules.invalidate(GUILD)
        self.assertTrue(modules.set_enabled(GUILD, FLAG, False))
        modules.invalidate(GUILD)
        self.assertFalse(modules.is_enabled(GUILD, FLAG))

        asyncio.run(db.setup())

        modules.invalidate(GUILD)
        self.assertFalse(modules.is_enabled(GUILD, FLAG), "the switch was turned back on")
        self.assertEqual(self._legacy_rows(), {GUILD: False})

    def test_setup_writes_the_marker_so_a_fresh_database_is_never_carried(self):
        """The marker itself — and the state a server that never used either gets.

        ``setUp`` deletes the marker, which is the database this rule was written
        for; booting again is what puts it back.
        """
        self.assertIsNone(self._marker(), "the fixture left the marker in place")
        asyncio.run(db.setup())
        self.assertIsNotNone(self._marker(), "db.setup() did not write the migration marker")
        self.assertEqual(self._legacy_rows(), {})


class TheServedState(TempDatabaseMixin, unittest.TestCase):
    """The half a member meets: the gate refuses, and says who can change it."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        modules.invalidate()

    def tearDown(self):
        modules.invalidate()
        self.release_temp_database()

    def test_the_gate_reads_the_flag_and_refuses_by_default(self):
        self.assertFalse(modules.is_enabled(GUILD, FLAG))
        self.assertTrue(modules.set_enabled(GUILD, FLAG, True))
        modules.invalidate(GUILD)
        self.assertTrue(modules.is_enabled(GUILD, FLAG))

    def test_a_refusal_in_the_cogs_names_the_switch(self):
        """Read from the source so the two cogs cannot drift apart on the wording."""
        for relative in ("cogs/radio.py", "cogs/qna.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("/bot legacy state:On", source, relative)
            self.assertTrue(
                re.search(r"legacy module\*\*", source),
                f"{relative} no longer says which module refused it",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
