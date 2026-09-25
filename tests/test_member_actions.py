"""Member actions from the website: the guard, the record, and the roster.

The module exists because a second caller of ``/ban`` is how a product ends up
with two answers to "what happened". So the tests care about the three things the
two doors must agree on — who may be touched at all, whether a refused action is
written down as refused, and whether a web action can be told from a command in
the record — and about the roster being a *reading* rather than an invention.
"""
import datetime
import os
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import db, moderation_actions as actions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# The smallest objects that can answer the questions these modules ask
# --------------------------------------------------------------------------- #
class _Perms:
    def __init__(self, **flags):
        self.administrator = bool(flags.get("administrator"))
        self.manage_channels = flags.get("manage_channels", True)
        self.send_messages = flags.get("send_messages", True)
        self.create_public_threads = True
        self.add_reactions = True

    def __getattr__(self, name):  # a permission nobody set is absent, not a crash
        return False


class _Role:
    def __init__(self, name, position=1):
        self.name = name
        self.position = position
        self.id = 7

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)


class _Avatar:
    url = "https://cdn.test/a.png"


class Member:
    """A member, with the attributes the guard and the actions actually read."""

    def __init__(self, member_id, name="Someone", *, guild=None, permissions=None,
                 top_role=None, bot=False, timed_out_until=None):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.bot = bot
        self.guild = guild
        self.guild_permissions = permissions or _Perms()
        self.top_role = top_role or _Role("Member", 1)
        self.display_avatar = _Avatar()
        self.created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        self.joined_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        self.timed_out_until = timed_out_until
        self.roles = [_Role("Member", 1), _Role("Cool", 3)]
        self.calls = []

    async def timeout(self, delta, *, reason=""):
        self.calls.append(("timeout", delta, reason))
        self.timed_out_until = None if delta is None else datetime.datetime.now(datetime.timezone.utc) + delta

    async def kick(self, *, reason=""):
        self.calls.append(("kick", reason))

    async def ban(self, *, reason="", delete_message_days=0):
        self.calls.append(("ban", reason, delete_message_days))


class Channel:
    def __init__(self, channel_id, name="general", *, kind="text", overwrites=None, manage=True):
        self.id = channel_id
        self.name = name
        self.type = kind
        self.overwrites = dict(overwrites or {})
        self.guild = None
        self._manage = manage
        self.calls = []

    def permissions_for(self, _member):
        return _Perms(manage_channels=self._manage)

    async def set_permissions(self, target, *, overwrite=None, reason=""):
        self.calls.append(("set_permissions", target, overwrite, reason))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def edit(self, *, slowmode_delay=0, reason=""):
        self.calls.append(("edit", slowmode_delay, reason))


class Guild:
    def __init__(self, guild_id=424242, *, channels=(), owner_id=999, members=()):
        self.id = guild_id
        self.name = "Test Server"
        self.owner_id = owner_id
        self.default_role = _Role("@everyone", 0)
        self.channels = list(channels)
        self.members = list(members)
        self.chunked = True
        self.me = None
        self.actions = []
        for channel in self.channels:
            channel.guild = self

    async def query_members(self, *, limit=0):
        return list(self.members)

    async def fetch_user(self, user_id):
        return Member(user_id, "Fetched")

    async def unban(self, user, *, reason=""):
        self.actions.append(("unban", getattr(user, "id", 0), reason))


class TheGuard(unittest.TestCase):
    """One rule, and it is the one a web panel is most likely to get wrong."""

    def setUp(self):
        self.guild = Guild()
        self.actor = Member(1, "Admin", guild=self.guild, permissions=_Perms(administrator=True))
        self.guild.owner_id = 999

    def test_it_refuses_yourself(self):
        self.assertIn("yourself", actions.target_problem(self.actor, self.actor))

    def test_it_refuses_the_server_owner(self):
        owner = Member(self.guild.owner_id, "Owner", guild=self.guild, permissions=_Perms(administrator=True))
        self.assertIn("server owner", actions.target_problem(self.actor, owner))

    def test_it_refuses_the_bot_itself(self):
        self.guild.me = Member(55, "Meow", guild=self.guild, bot=True)
        self.assertIn("me", actions.target_problem(self.actor, self.guild.me))

    def test_it_refuses_an_administrator_to_a_non_administrator(self):
        plain = Member(2, "Helper", guild=self.guild, permissions=_Perms())
        target = Member(3, "Boss", guild=self.guild, permissions=_Perms(administrator=True))
        self.assertIn("administrator", actions.target_problem(plain, target))

    def test_it_refuses_a_role_at_or_above_yours(self):
        plain = Member(2, "Helper", guild=self.guild, top_role=_Role("Helper", 2))
        target = Member(3, "Peer", guild=self.guild, top_role=_Role("Peer", 5))
        self.assertIn("at or above", actions.target_problem(plain, target))

    def test_the_owner_may_moderate_anybody(self):
        self.guild.owner_id = self.actor.id
        target = Member(3, "Loud", guild=self.guild, top_role=_Role("Higher", 9))
        self.assertIsNone(actions.target_problem(self.actor, target))

    def test_an_unreadable_target_is_refused_rather_than_assumed_fine(self):
        self.assertIn("could not be read", actions.target_problem(self.actor, None))

    def test_the_cog_still_uses_this_same_guard(self):
        """The commands must not grow their own copy of the rule.

        Moved out of ``cogs/moderation.py`` when the website became a second
        caller: a second implementation is how a panel ends up able to do what a
        command cannot, and the mutation this guards against is somebody pasting
        the function back in because the import looked inconvenient.
        """
        source = open(os.path.join(ROOT, "cogs", "moderation.py"), encoding="utf-8").read()
        self.assertIn("target_problem", source)
        self.assertNotIn("def _mod_target_problem", source, "the guard has two definitions again")


class TheRecord(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        # The schema, because these tests are about rows landing in tables: a
        # database without them answers "no such table" and every assertion
        # about the record would be testing the wrong failure.
        await db.setup()

    def tearDown(self):
        self.release_temp_database()

    async def _log_calls(self):
        """Capture what reached the server's moderation log."""
        posted = []

        async def fake_post(guild, embed, *, what="case"):
            posted.append((embed, what))
            return True

        return posted, fake_post

    async def test_a_ban_opens_one_case_and_writes_one_log_card(self):
        guild = Guild()
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        posted, fake_post = await self._log_calls()
        with mock.patch.object(actions.modlog, "post", new=fake_post):
            outcome = await actions.apply(actions.Request(guild=guild, action="ban", target=target, actor=actor, reason="spam"))
        self.assertTrue(outcome.ok)
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(target.calls), 1)
        self.assertIn("website", target.calls[0][1], "the audit reason names where the order came from")
        cases = await self._cases(guild.id)
        self.assertEqual([row[0] for row in cases], ["Ban"])

    async def test_a_refused_action_is_not_a_case_and_says_so_in_the_log(self):
        """The automod rule, applied here: a log where a refusal is silent reads
        like a filter that worked."""
        import discord

        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")
        posted, fake_post = await self._log_calls()

        async def refuse(*_args, **_kwargs):
            raise discord.Forbidden(mock.Mock(status=403), "no perms")

        with mock.patch.object(target, "kick", new=refuse), \
                mock.patch.object(actions.modlog, "post", new=fake_post), \
                mock.patch.object(actions, "failure_note", actions.failure_note):
            outcome = await actions.apply(actions.Request(guild=guild, action="kick", target=target, actor=actor, reason="bye"))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refused_by, "discord")
        self.assertIn("Kick Members", outcome.detail, "the refusal names the permission to grant")
        self.assertEqual(await self._cases(guild.id), [], "a refusal is not a case")
        self.assertEqual(len(posted), 1, "the refusal is written down")
        self.assertIn("not", posted[0][0].title.lower() + posted[0][0].description.lower() + str(outcome.detail).lower())

    async def test_a_timeout_is_clamped_and_the_reason_says_how_long(self):
        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")
        async def noop(*_a, **_k):
            return None
        with mock.patch.object(actions.modlog, "post", new=noop):
            outcome = await actions.apply(
                actions.Request(guild=guild, action="timeout", target=target, actor=actor, reason="flood", minutes=999999)
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.minutes, actions.TIMEOUT_MINUTES[1])
        delta, reason = target.calls[0][1], target.calls[0][2]
        self.assertEqual(delta, datetime.timedelta(minutes=actions.TIMEOUT_MINUTES[1]))
        self.assertIn("(40320m)", reason)

    async def test_a_warning_lands_in_the_warnings_table_and_a_clear_removes_it(self):
        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")

        async def noop(*_a, **_k):
            return None

        with mock.patch.object(actions.modlog, "post", new=noop):
            warned = await actions.apply(actions.Request(guild=guild, action="warn", target=target, actor=actor, reason="caps"))
            self.assertTrue(warned.ok)
            self.assertEqual(len(await db.get_warnings(guild.id, target.id)), 1)
            cleared = await actions.apply(actions.Request(guild=guild, action="clear_warnings", target=target, actor=actor))
        self.assertTrue(cleared.ok)
        self.assertEqual(await db.get_warnings(guild.id, target.id), [])

    async def test_warning_text_is_trimmed_rather_than_stored_whole(self):
        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")

        async def noop(*_a, **_k):
            return None

        with mock.patch.object(actions.modlog, "post", new=noop):
            await actions.apply(
                actions.Request(guild=guild, action="warn", target=target, actor=actor, reason="x" * 5000)
            )
        rows = await db.get_warnings(guild.id, target.id)
        self.assertEqual(len(rows[0]["reason"]), actions.MAX_REASON)

    async def test_an_actor_that_could_not_be_read_is_refused_not_waved_through(self):
        guild = Guild()
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=None, source="dashboard")
        outcome = await actions.apply(actions.Request(guild=guild, action="kick", target=target, actor=actor))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refused_by, "actor-unknown")
        self.assertEqual(target.calls, [], "nothing was attempted")

    async def test_an_unknown_action_is_refused_by_name(self):
        guild = Guild()
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild))
        outcome = await actions.apply(actions.Request(guild=guild, action="vaporise", target=Member(2), actor=actor))
        self.assertFalse(outcome.ok)
        self.assertIn("vaporise", outcome.detail)

    async def test_an_unban_needs_no_hierarchy_guard_and_still_records(self):
        guild = Guild()
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")
        posted, fake_post = await self._log_calls()
        with mock.patch.object(actions.modlog, "post", new=fake_post):
            outcome = await actions.apply(
                actions.Request(guild=guild, action="unban", target=Member(9, "Gone", guild=guild), actor=actor, reason="appeal won")
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(guild.actions[0][0], "unban")
        self.assertEqual([row[0] for row in await self._cases(guild.id)], ["Unban"])
        self.assertEqual(len(posted), 1)

    async def _cases(self, guild_id):
        import aiosqlite

        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT case_type, moderator_id FROM mod_cases WHERE guild_id = ? ORDER BY case_id", (guild_id,)
            ) as cursor:
                return [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]

    def _member(self, guild):
        # Above the target's role on purpose: the guard refuses an *equal* role,
        # which is Discord's own rule and the one the slash commands already
        # followed.
        return Member(
            1, "Admin", guild=guild, permissions=_Perms(administrator=True),
            top_role=_Role("Admin", 9),
        )

    async def test_the_website_asks_the_permission_question_the_command_asks(self):
        """A demoted administrator keeps a session; they must not keep the power.

        The dashboard resolves the actor from the session's own ``user_id``, and
        the call to Discord is made by **the bot** — so nothing on that path ever
        consulted the acting person's live permissions. ``/ban`` demands Ban
        Members of whoever typed it; the button that does the same thing has to
        demand it too, or the dashboard can do what a command cannot.
        """
        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        former = Member(
            1, "Former Admin", guild=guild, permissions=_Perms(), top_role=_Role("Member", 4)
        )
        actor = actions.Actor(id=1, name="Former Admin", member=former, source="dashboard")
        outcome = await actions.apply(
            actions.Request(guild=guild, action="ban", target=target, actor=actor, reason="spam")
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refused_by, "permission")
        self.assertEqual(target.calls, [], "nothing may reach Discord")
        self.assertIn("Ban Members", outcome.detail, "the refusal names the permission")

    async def test_an_administrator_holds_every_permission_at_once(self):
        """Discord's own rule, and the reason a server owner is never refused."""
        guild = Guild()
        guild.me = Member(55, "Meow", guild=guild, bot=True)
        target = Member(3, "Loud", guild=guild)
        actor = actions.Actor(id=1, name="Admin", member=self._member(guild), source="dashboard")
        posted, fake_post = await self._log_calls()
        with mock.patch.object(actions.modlog, "post", new=fake_post):
            outcome = await actions.apply(
                actions.Request(guild=guild, action="ban", target=target, actor=actor, reason="spam")
            )
        self.assertTrue(outcome.ok, outcome.detail)

    def test_every_action_requires_what_its_command_requires(self):
        """The table and the commands' own decorators, compared as a fact.

        Read out of ``cogs/moderation.py`` on purpose. The one thing that must
        never drift is which permission an action needs, and the drift is what
        caused this: the website asked nothing at all while every command asked
        Discord. A second hand-written copy of the mapping is how that happens
        again, so the two are compared rather than restated.
        """
        import re as _re
        from pathlib import Path as _Path

        source = (_Path(__file__).resolve().parent.parent / "cogs" / "moderation.py").read_text()
        declared = {
            match.group(2): match.group(1)
            for match in _re.finditer(
                r"@app_commands\.checks\.has_permissions\((\w+)=True\)\s*\n\s*async def (\w+)\(",
                source,
            )
        }
        self.assertGreater(len(declared), 4, "the commands' permissions were not found")
        command_for_action = {
            "warn": "warn",
            "timeout": "mute",
            "timeout_remove": "unmute",
            "kick": "kick",
            "ban": "ban",
            "unban": "unban",
            "clear_warnings": "clearwarnings",
        }
        for action, command in command_for_action.items():
            with self.subTest(action=action):
                self.assertEqual(
                    actions.ACTOR_PERMISSIONS.get(action),
                    declared.get(command),
                    f"the website and /{command} must demand the same permission",
                )


class TheRoster(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild(members=[
            Member(1, "Alice", guild=None),
            Member(2, "Bob", guild=None, bot=True),
            Member(3, "Charlie", guild=None),
        ])
        for member in self.guild.members:
            member.guild = self.guild

    async def test_it_matches_ids_names_and_nicknames(self):
        found = await actions.roster(self.guild, "char")
        self.assertEqual([row["name"] for row in found["members"]], ["Charlie"])
        by_id = await actions.roster(self.guild, "1")
        self.assertEqual([row["name"] for row in by_id["members"]], ["Alice"])
        self.assertEqual(by_id["matched"], 1)

    async def test_it_can_leave_the_robots_out(self):
        everyone = await actions.roster(self.guild)
        humans = await actions.roster(self.guild, with_bots=False)
        self.assertEqual(everyone["matched"], 3)
        self.assertEqual(humans["matched"], 2)

    async def test_an_empty_cache_asks_discord_rather_than_reporting_nobody(self):
        class _Cold(Guild):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.loaded = list(self.members)
                self.members = []
                self.chunked = False

            async def query_members(self, *, limit=0):
                self.queried = True
                return self.loaded

        guild = _Cold(members=[Member(1, "Alice")])
        found = await actions.roster(guild, "ali")
        self.assertTrue(found["queried"])
        self.assertEqual(found["matched"], 1)
        self.assertTrue(found["complete"])

    async def test_an_empty_search_answer_is_not_a_claim_that_the_server_is_empty(self):
        class _Cold(Guild):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.members = []
                self.chunked = False

            async def query_members(self, *, limit=0):
                raise RuntimeError("query failed")

        found = await actions.roster(_Cold())
        self.assertFalse(found["complete"], "the page must be able to say the list is partial")
        self.assertEqual(found["cache"], 0)

    async def test_a_row_carries_ids_as_text_and_a_real_timeout(self):
        guild = Guild()
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
        member = Member(123456789012345678, "Timed", guild=guild, timed_out_until=until)
        row = actions.member_row(member)
        self.assertIsInstance(row["id"], str)
        self.assertGreater(row["timeout_seconds"], 500)
        self.assertLess(row["timeout_seconds"], 700)
        self.assertEqual(row["roles"][0]["id"], "7")

    async def test_the_ban_list_is_read_through_an_iterator_and_survives_refusal(self):
        class _Banned(Guild):
            async def bans(self, *, limit=0):
                for entry in (mock.Mock(user=Member(5, "Naughty"), reason="spam"),):
                    yield entry

        rows = await actions.banned(_Banned())
        self.assertEqual(rows[0]["name"], "Naughty")
        self.assertEqual(rows[0]["reason"], "spam")
        import discord

        class _Refused(Guild):
            def bans(self, *, limit=0):
                raise discord.Forbidden(mock.Mock(status=403), "nope")

        self.assertEqual(await actions.banned(_Refused()), [], "no permission is a reading, not a crash")


class TheCommandsUseOneDoor(unittest.TestCase):
    """A source-reading guard, on the same rule as the automod record's.

    The web panel and the slash commands must reach Discord and the log through
    the same two functions. This fails on the shape that would break it: a
    moderation command that performs its own Discord write.
    """

    def test_every_moderation_command_goes_through_the_shared_record(self):
        source = open(os.path.join(ROOT, "cogs", "moderation.py"), encoding="utf-8").read()
        self.assertIn("moderation_actions.record(", source, "the log card has two builders again")
        self.assertNotIn("async def _log_embed", source, "the cog grew its own second card back")
        for write in ("await member.ban(", "await member.kick(", "await member.timeout("):
            self.assertNotIn(
                f"            {write}", source,
                f"{write} is called directly — the record and the action have drifted apart",
            )

    def test_the_cog_holds_no_sql_of_its_own(self):
        """The warning rows moved behind ``utils.db`` when the website became a
        second writer: two copies of "what a warning is" is how a panel and a
        command end up disagreeing about whether somebody has one."""
        source = open(os.path.join(ROOT, "cogs", "moderation.py"), encoding="utf-8").read()
        self.assertNotIn("INSERT INTO warnings", source)
        self.assertNotIn("DELETE FROM warnings", source)
        self.assertIn("db.get_warnings(", source)
        self.assertIn("db.clear_warnings(", source)
        module = open(os.path.join(ROOT, "utils", "moderation_actions.py"), encoding="utf-8").read()
        self.assertIn("db.add_warning(", module)


if __name__ == "__main__":
    unittest.main()
