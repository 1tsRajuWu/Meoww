"""What `/check` answers: the holes Discord's own screens do not draw.

The report this command came from is the shape of this whole file — *a private
channel a normal role can still see*, *a locked channel somebody can still type
in* — and the two are one rule of Discord's model: an allow on any overwrite
besides `@everyone` is applied after `@everyone`'s, so it wins. Four things are
pinned here, and the first is the reason the suite exists at all:

* **The reading is a fact, not a name.** A channel is private when `@everyone`
  denies `View Channel`, read through the **same pair reader the lock writes
  through** — ``pair()``, never an ``allow`` attribute, which is how the first
  version of that reader did nothing on real Discord while passing every law. One
  law here builds a real ``discord.PermissionOverwrite`` for exactly that reason.
* **One hole is one finding.** A private room open to two roles is one sentence
  naming both, not two rows; and a channel the lockdown ledger holds is the lock
  check's business rather than a second opinion from the visibility list.
* **`fix` cannot wander.** The only channels it rewrites are the ones the ledger
  already says are locked, and the laws drive the **real command callback** to
  prove it — including that re-locking does not overwrite the row's undo.
* **What could not be read is said, not swallowed.** An unreadable ledger is a
  count on the card, because *no holes found* answered without looking is the
  failure this module is written against.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from tests.support import TempDatabaseMixin
from tests.test_server_control import (
    ALL_LOCKED,
    Actor,
    Bits,
    Channel,
    Guild,
    Overwrite,
    Perms,
    Role,
)
from utils import db, security_check, server_control

GUILD = 424242
BOT = 424242


class _Me:
    """The bot's own member — and an overwrite key, so it has to be hashable."""

    def __init__(self, *, id=BOT, guild_permissions=None, top_role=None, name="Meow"):
        self.id = id
        self.name = name
        self.guild_permissions = guild_permissions
        self.top_role = top_role


def _me(*, missing=(), top=None, id=BOT):
    """The bot's own member: everything it needs, minus what a law takes away."""
    needed = {name: True for name, _breaks in security_check.BOT_NEEDS}
    for name in missing:
        needed[name] = False
    return _Me(id=id, guild_permissions=Perms(**needed), top_role=top)


def _hardened(guild):
    """The three server-safety switches at a safe setting.

    A law about one finding should not carry four more that nobody asked about —
    and a real server with the filter off *should* be told, which is why these are
    set rather than removed from the engine. The values are **Discord's own enum
    objects**, not ints and strings that stand in for them: the first version of
    `mfa_level` here was the int ``1``, and the engine's ``int()`` on the real
    enum value raised ``TypeError`` on every live `/check`.
    """
    guild.verification_level = discord.VerificationLevel.medium
    guild.explicit_content_filter = discord.ContentFilter.all_members
    guild.mfa_level = discord.MFALevel.require_2fa
    return guild


class _Bot:
    """A bot as much as the audit reads one: the gate, the intent, my own user."""

    def __init__(self, *, automod=True, members=True):
        self.intents = SimpleNamespace(members=members)
        self.user = SimpleNamespace(id=BOT, bot=True)
        self.guilds = []
        self._automod = automod

    def is_module_enabled(self, _guild_id, module):
        return self._automod if module == "automod" else True

    async def wait_until_ready(self):
        return True


def _attached(channel):
    """A channel that knows its guild — which is how it finds its `@everyone`."""
    guild = Guild(guild_id=GUILD)
    channel.guild = guild
    guild.channels = [channel]
    return guild, channel


def _settings(values):
    """``db.get_setting`` answering one fixed set — the shape the audit reads."""
    async def read(_guild_id, key, default=None):
        return values.get(key, default)

    return read


class TheVisibilityNobodyDraws(unittest.TestCase):
    """*Who can actually see this room* — the question with no page in Discord."""

    def test_a_private_channel_open_to_a_role_is_one_finding_naming_the_role(self):
        guild, vault = _attached(Channel(1, "vault"))
        normal = Role("Normal", id=7)
        guild.roles.append(normal)
        vault.overwrites[guild.default_role] = Overwrite(deny=Bits.NAMES["view_channel"])
        vault.overwrites[normal] = Overwrite(allow=Bits.NAMES["view_channel"])

        found = security_check.visibility_notes(guild, [vault])

        self.assertEqual(len(found), 1, "the room the report is about was not reported")
        self.assertEqual(found[0].level, security_check.NOTE)
        self.assertEqual(found[0].where, "#vault")
        self.assertIn("@Normal", found[0].title)
        self.assertIn("private", found[0].title)

    def test_two_roles_are_one_sentence_rather_than_two_findings(self):
        guild, vault = _attached(Channel(1, "vault"))
        first, second = Role("Normal", id=7), Role("Staff", id=8)
        guild.roles.extend([first, second])
        vault.overwrites[guild.default_role] = Overwrite(deny=Bits.NAMES["view_channel"])
        vault.overwrites[first] = Overwrite(allow=Bits.NAMES["view_channel"])
        vault.overwrites[second] = Overwrite(allow=Bits.NAMES["view_channel"])

        found = security_check.visibility_notes(guild, [vault])

        self.assertEqual(len(found), 1)
        self.assertIn("@Normal", found[0].title)
        self.assertIn("@Staff", found[0].title)

    def test_a_private_channel_only_the_bot_can_see_is_not_a_finding(self):
        guild, vault = _attached(Channel(1, "vault"))
        vault.overwrites[guild.default_role] = Overwrite(deny=Bits.NAMES["view_channel"])
        vault.overwrites[guild.me] = Overwrite(allow=Bits.NAMES["view_channel"])
        guild.me = _me()

        self.assertEqual(security_check.visibility_notes(guild, [vault]), [])

    def test_a_public_channel_is_nobody_s_business_even_with_role_grants(self):
        guild, general = _attached(Channel(1, "general"))
        normal = Role("Normal", id=7)
        general.overwrites[normal] = Overwrite(allow=Bits.NAMES["view_channel"])

        self.assertEqual(security_check.visibility_notes(guild, [general]), [])

    def test_a_channel_the_ledger_holds_is_left_to_the_lock_check(self):
        guild, vault = _attached(Channel(1, "vault"))
        normal = Role("Normal", id=7)
        vault.overwrites[guild.default_role] = Overwrite(deny=Bits.NAMES["view_channel"])
        vault.overwrites[normal] = Overwrite(allow=Bits.NAMES["view_channel"])

        self.assertEqual(
            security_check.visibility_notes(guild, [vault], locked={1}),
            [],
            "the lock check answers this one, with the repair beside it",
        )

    def test_the_private_bit_is_read_from_discords_own_overwrite(self):
        """The exact misreading that made the previous pass a no-op on Discord."""
        guild, vault = _attached(Channel(1, "vault"))
        vault.overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

        self.assertTrue(security_check.claims_private(vault))

        vault.overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
        self.assertFalse(security_check.claims_private(vault))


class TheEveryonePower(unittest.TestCase):
    """What `@everyone` may do, server-wide and in one room."""

    def test_administrator_on_everyone_is_critical_and_says_why(self):
        guild, general = _attached(Channel(1, "general"))
        guild.default_role.permissions = Perms(manage_channels=False, administrator=True)

        found = security_check.everyone_power(guild)

        self.assertEqual([f.level for f in found], [security_check.CRITICAL])
        self.assertIn("administrator", found[0].title)
        self.assertIn("every channel overwrite", found[0].title)

    def test_send_messages_on_everyone_is_the_normal_state_of_a_community(self):
        guild, general = _attached(Channel(1, "general"))
        guild.default_role.permissions = Perms(manage_channels=False, send_messages=True)

        self.assertEqual(security_check.everyone_power(guild), [])

    def test_the_warning_and_note_tiers_are_the_ones_declared(self):
        guild, general = _attached(Channel(1, "general"))
        guild.default_role.permissions = Perms(
            manage_channels=False, create_instant_invite=True, manage_expressions=True
        )

        found = security_check.everyone_power(guild)

        self.assertEqual(
            {f.title.split("**")[1]: f.level for f in found},
            {
                "create instant invite": security_check.WARNING,
                "manage expressions": security_check.NOTE,
            },
        )

    def test_a_channel_hands_out_power_the_role_does_not(self):
        guild, general = _attached(Channel(1, "general"))
        general.overwrites[guild.default_role] = discord.PermissionOverwrite(manage_webhooks=True)

        found = security_check.channel_power(guild, [general])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, security_check.CRITICAL)
        self.assertIn("manage webhooks", found[0].title)
        self.assertEqual(found[0].where, "#general")

    def test_a_role_s_own_channel_grant_is_not_the_everyone_overwrite(self):
        guild, general = _attached(Channel(1, "general"))
        normal = Role("Normal", id=7)
        general.overwrites[normal] = Overwrite(allow=Bits.NAMES["send_messages"])

        self.assertEqual(security_check.channel_power(guild, [general]), [])

    def test_channel_power_is_read_from_discords_own_overwrite(self):
        guild, general = _attached(Channel(1, "general"))
        general.overwrites[guild.default_role] = discord.PermissionOverwrite(manage_webhooks=True)

        found = security_check.channel_power(guild, [general])

        self.assertEqual([f.level for f in found], [security_check.CRITICAL])


class TheLocksTheLedgerHolds(unittest.TestCase):
    """The repair list, and the two ways a row and a deny can disagree."""

    def _locked(self, *, hide=False, leak=0):
        everyone = Role("@everyone")
        quiet = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})
        if hide:
            quiet.overwrites[everyone] = Overwrite(
                deny=ALL_LOCKED | Bits.NAMES["view_channel"]
            )
        normal = Role("Normal", id=7)
        if leak:
            quiet.overwrites[normal] = Overwrite(allow=leak)
        guild = Guild(channels=[quiet], roles=[normal])
        guild.default_role = everyone
        return guild, quiet, normal

    def test_a_lock_a_role_can_type_through_is_critical_and_on_the_repair_list(self):
        guild, quiet, _normal = self._locked(leak=Bits.NAMES["send_messages"])

        found, beaten, loose = security_check.lockdown_findings(guild, [quiet], {1: "channel"})

        self.assertEqual([f.level for f in found], [security_check.CRITICAL])
        self.assertEqual(found[0].where, "#quiet")
        self.assertIn("@Normal", found[0].title)
        self.assertEqual(beaten, [quiet])
        self.assertEqual(loose, [])

    def test_a_lock_nobody_can_get_through_is_left_alone(self):
        guild, quiet, _normal = self._locked()

        found, beaten, loose = security_check.lockdown_findings(guild, [quiet], {1: "channel"})

        self.assertEqual((found, beaten, loose), ([], [], []))

    def test_a_private_lock_asks_about_visibility_as_well_as_voice(self):
        """`hide:True` denies `View Channel`, so a role that can still see the
        room is a role that can get in — the second half of the report."""
        guild, quiet, _normal = self._locked(hide=True, leak=Bits.NAMES["view_channel"])

        found, beaten, _loose = security_check.lockdown_findings(guild, [quiet], {1: "channel"})

        self.assertEqual([f.level for f in found], [security_check.CRITICAL])
        self.assertEqual(beaten, [quiet])

    def test_a_lock_whose_deny_is_gone_is_loose_rather_than_beaten(self):
        everyone = Role("@everyone")
        quiet = Channel(1, "quiet")
        guild = Guild(channels=[quiet])
        guild.default_role = everyone

        found, beaten, loose = security_check.lockdown_findings(guild, [quiet], {1: "channel"})

        self.assertEqual(beaten, [])
        self.assertEqual(loose, [quiet])
        self.assertIn("deny is gone", found[0].title)
        self.assertIn("`/check fix:True`", found[0].detail)

    def test_a_lock_the_ledger_does_not_hold_is_reported_but_never_repaired(self):
        """Only the audit log may claim an unrecorded lock, so `fix` cannot."""
        everyone = Role("@everyone")
        old = Channel(1, "old", overwrites={everyone: Overwrite(deny=Bits.NAMES["send_messages"])})
        guild = Guild(channels=[old])
        guild.default_role = everyone

        found, beaten, loose = security_check.lockdown_findings(guild, [old], {})

        self.assertEqual([f.title for f in found], ["looks locked and is not enforced"])
        self.assertEqual((beaten, loose), ([], []))

    def test_a_ledger_row_for_a_channel_discord_does_not_send_is_named(self):
        guild = Guild(channels=[Channel(1, "general")])

        found, _beaten, _loose = security_check.lockdown_findings(guild, guild.channels, {99: "channel"})

        self.assertEqual(found[0].where, "channel 99")
        self.assertIn("does not send it to me", found[0].title)

    def test_the_bot_s_own_overwrite_is_never_a_leak(self):
        everyone = Role("@everyone")
        quiet = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})
        guild = Guild(channels=[quiet])
        guild.default_role = everyone
        guild.me = _me()
        quiet.overwrites[guild.me] = Overwrite(allow=Bits.NAMES["send_messages"])

        found, beaten, _loose = security_check.lockdown_findings(guild, [quiet], {1: "channel"})

        self.assertEqual((found, beaten), ([], []))


class TheBotStanding(unittest.TestCase):
    """What this bot cannot do here, in the symptom rather than the bit."""

    def test_a_missing_permission_is_reported_as_what_stops_working(self):
        guild, general = _attached(Channel(1, "general"))
        guild.me = _me(missing=("manage_messages",))

        found = security_check.bot_standing(guild)

        self.assertEqual(len(found), 1)
        self.assertIn("manage messages", found[0].title)
        self.assertIn("sweep cannot remove", found[0].title)

    def test_a_role_above_mine_is_a_note_about_who_i_cannot_act_on(self):
        guild, general = _attached(Channel(1, "general"))
        above = Role("Owner", position=9, id=9, manage_channels=False)
        above.permissions = SimpleNamespace(manage_roles=True)
        guild.roles.append(above)
        guild.me = _me(top=Role("Meow", position=3, id=BOT))

        found = security_check.bot_standing(guild)

        self.assertEqual([f.level for f in found], [security_check.NOTE])
        self.assertIn("@Owner", found[0].title)

    def test_a_guild_that_does_not_describe_my_member_reports_nothing(self):
        """Unreadable is not a refusal: a stand-in with no permission answer must
        not invent eight findings about a bot nobody asked Discord about."""
        guild, general = _attached(Channel(1, "general"))

        self.assertEqual(security_check.bot_standing(guild), [])


class TheSettingsTheServerKeeps(unittest.IsolatedAsyncioTestCase):
    """The switches and settings the moderation half depends on."""

    def _guild(self, *, log_channel=None):
        guild = _hardened(Guild(channels=[Channel(1, "general")]))
        guild.me = _me()
        if log_channel is not None:
            guild.channels.append(log_channel)
            log_channel.guild = guild
        return guild

    async def _findings(self, guild, *, values, bot=None):
        # A bot with the Members intent on and the readers below, so a law about
        # one setting is not also a law about *the intent is off*.
        with mock.patch.object(db, "get_setting", new=_settings(values)):
            return await security_check.settings_findings(guild, bot=bot or _Bot())

    async def test_no_log_channel_is_a_warning(self):
        guild = self._guild()

        found = await self._findings(guild, values={"default_role_id": 9})

        self.assertEqual([f.level for f in found], [security_check.WARNING])
        self.assertIn("is not set", found[0].title)

    async def test_a_log_channel_that_is_gone_is_a_warning(self):
        guild = self._guild()

        found = await self._findings(guild, values={"log_channel_id": 77})

        self.assertIn("channel that is gone", found[0].title)

    async def test_a_log_channel_i_cannot_post_in_is_a_warning(self):
        guild = self._guild(log_channel=Channel(5, "logs"))

        found = await self._findings(
            guild, values={"log_channel_id": 5, "default_role_id": 9}
        )

        self.assertEqual([f.level for f in found], [security_check.WARNING])
        self.assertIn("I cannot post in it", found[0].title)

    async def test_a_log_channel_i_can_post_in_is_quiet(self):
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild = self._guild(log_channel=logs)

        self.assertEqual(
            await self._findings(
                guild, values={"log_channel_id": 5, "default_role_id": 9}
            ),
            [],
        )

    async def test_a_switch_that_is_off_is_a_warning(self):
        guild = self._guild()
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild.channels.append(logs)
        logs.guild = guild

        found = await self._findings(
            guild, values={"log_channel_id": 5, "default_role_id": 9}, bot=_Bot(automod=False)
        )

        self.assertEqual([f.level for f in found], [security_check.WARNING])
        self.assertIn("is switched off", found[0].title)

    async def test_the_members_intent_off_is_a_note_even_with_the_default_role_set(self):
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild = self._guild(log_channel=logs)

        found = await self._findings(
            guild, values={"log_channel_id": 5, "default_role_id": 9}, bot=_Bot(members=False)
        )

        self.assertEqual([f.level for f in found], [security_check.NOTE])
        self.assertIn("Members intent", found[0].title)

    async def test_a_missing_default_role_is_a_note_when_the_intent_can_deliver_it(self):
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild = self._guild(log_channel=logs)

        found = await self._findings(guild, values={"log_channel_id": 5}, bot=_Bot())

        self.assertEqual([f.level for f in found], [security_check.NOTE])
        self.assertIn("is not set", found[0].title)

    async def test_the_enum_discord_really_sends_is_read_without_int(self):
        """The second fake-vs-real defect in this file, found live the same way.

        ``guild.mfa_level`` is an enum **value** in discord.py 2.4+ and it has
        **no ``__int__``**, so the engine's ``int()`` raised ``TypeError: int()
        argument must be a string, a bytes-like object or a real number, not
        '_EnumValue_MFALevel'`` — every real `/check` answered *an unexpected
        error occurred* while every law here passed, because the fakes answered
        the plain int ``1``. It is also **truthy even when it is ``disabled``**
        (``bool`` is the object) and ``disabled == 1`` is False, so neither
        ``bool()`` nor ``== 1`` would have been honest either: ``.value`` is the
        only read that is.
        """
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild = self._guild(log_channel=logs)
        settings = {"log_channel_id": 5, "default_role_id": 9}

        guild.mfa_level = discord.MFALevel.require_2fa
        self.assertEqual(
            await self._findings(guild, values=settings), [],
            "2FA is required and the card said it was not",
        )

        guild.mfa_level = discord.MFALevel.disabled
        found = await self._findings(guild, values=settings)
        self.assertEqual([f.level for f in found], [security_check.NOTE])
        self.assertIn("2FA", found[0].title)

        guild.mfa_level = 1  # the older discord.py shape: the int itself
        self.assertEqual(await self._findings(guild, values=settings), [])

    async def test_an_unreadable_setting_is_named_rather_than_invented(self):
        guild = self._guild()

        async def broken(_guild_id, _key, default=None):
            raise RuntimeError("the database is not answering")

        with mock.patch.object(db, "get_setting", new=broken):
            found = await security_check.settings_findings(guild, bot=_Bot())

        self.assertEqual([f.title for f in found], ["is not set"])


class TheAudit(unittest.IsolatedAsyncioTestCase):
    """The whole reading, and the count of what it could not read."""

    def _clean(self):
        guild, _channel = _attached(Channel(1, "general"))
        _hardened(guild)
        guild.me = _me()
        logs = Channel(5, "logs")
        logs.permissions_for = lambda _me: Perms(send_messages=True, embed_links=True)
        guild.channels.append(logs)
        logs.guild = guild
        return guild

    async def test_a_clean_server_says_so_and_counts_what_it_walked(self):
        guild = self._clean()

        with mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot())

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.checked, 2)
        self.assertIn("No holes found in **2** channel(s).", report.sentence())

    async def test_a_hole_and_a_switch_are_both_on_one_card(self):
        guild = self._clean()
        guild.default_role.permissions = Perms(manage_channels=False, administrator=True)

        with mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot(automod=False))

        self.assertFalse(report.ok)
        self.assertEqual([f.level for f in report.findings], [security_check.CRITICAL, security_check.WARNING])
        self.assertIn("**1** critical", report.sentence())
        self.assertIn("**1** warning", report.sentence())

    async def test_findings_come_back_worst_first(self):
        guild = self._clean()
        guild.default_role.permissions = Perms(
            manage_channels=False, ban_members=True, manage_expressions=True
        )

        with mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot())

        ordered = [security_check.LEVELS.index(f.level) for f in report.findings]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(report.worst, security_check.WARNING)

    async def test_a_ledger_that_cannot_be_read_is_counted_not_swallowed(self):
        guild = self._clean()

        async def broken(_guild_id):
            raise RuntimeError("no ledger")

        with mock.patch.object(server_control, "locked_channels", new=broken), mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot())

        self.assertEqual(report.unread, 1)
        self.assertIn("could not be read", report.sentence())

    async def test_the_dict_is_the_same_answer_in_a_second_shape(self):
        guild = self._clean()
        guild.default_role.permissions = Perms(manage_channels=False, administrator=True)

        with mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot())

        said = report.to_dict()
        self.assertFalse(said["ok"])
        self.assertEqual(said["guild_id"], str(GUILD))
        self.assertEqual(said["counts"], report.counts)
        self.assertEqual(said["findings"][0]["level"], security_check.CRITICAL)
        self.assertEqual(said["said"], report.sentence())


class TheCommandAndItsRepair(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """`/check` driven through its real callback, with the real ledger.

    A read is the whole command; the one write it can make is `fix`, and the laws
    below are about where that write is allowed to land: a channel the ledger
    already holds, never a room by guesswork — and never at the price of the undo
    the row is there for.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()
        from cogs.server_control import ServerControl

        self.bot = _Bot()
        self.cog = ServerControl(bot=self.bot)
        self.channel = Channel(1, "premier")
        # The channel is also the log channel these laws pin quiet, so it has to be
        # one the bot could post a case in.
        self.channel.permissions_for = lambda _me: Perms(
            manage_channels=True, manage_messages=True, moderate_members=True,
            send_messages=True, embed_links=True,
        )
        self.guild = _hardened(Guild(channels=[self.channel]))
        self.guild.me = _me()
        self.interaction = _FakeCheck(self.guild, self.channel)

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _said(self) -> str:
        payloads = self.interaction.response.messages + self.interaction.followup.messages
        return " ".join(_payload_text(payload) for payload in payloads)

    def _quiet_settings(self):
        """The settings that are not what these laws are about, at their set state."""
        return mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 1, "default_role_id": 9})
        )

    async def _lock(self, *, hide=False):
        """A real lock through the real ledger, so the row under test is real."""
        await server_control.lock_channel(self.guild, self.channel, Actor(), hide=hide)

    async def _lock_then_leak(self, *, view=False):
        """The state the report is about: a real lock, and a role let in after it."""
        normal = Role("Normal", id=7)
        self.guild.roles.append(normal)
        self.normal = normal
        await self._lock(hide=view)
        self.channel.overwrites[normal] = Overwrite(
            allow=Bits.NAMES["view_channel"] if view else Bits.NAMES["send_messages"]
        )
        return normal

    async def test_the_card_reports_the_hole_and_fix_repairs_it(self):
        from cogs.server_control import ServerControl

        await self._lock_then_leak()
        before = await db.lockdown_rows(self.guild.id)

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction, True)

        said = self._said()
        self.assertIn("1 finding(s)", said)
        self.assertIn("🔴 Open right now", said)
        self.assertIn("@Normal", said)
        self.assertIn("Repaired", said)
        self.assertIn("wrote the deny again on **1** channel(s)", said)
        allow, deny = self.channel.overwrites[self.normal].pair()
        self.assertTrue(deny.send_messages, "the allow that beat the lock is still there")
        self.assertEqual(
            await db.lockdown_rows(self.guild.id), before,
            "re-locking rewrote the row, so /unlock would restore the lock",
        )

    async def test_a_private_channel_is_denied_visibility_when_it_is_repaired(self):
        """The second half of the report: `hide` follows the channel's own state."""
        from cogs.server_control import ServerControl

        await self._lock_then_leak(view=True)

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction, True)

        _allow, deny = self.channel.overwrites[self.normal].pair()
        self.assertTrue(deny.view_channel, "a role that could still see it still can")

    async def test_the_reading_alone_changes_nothing(self):
        from cogs.server_control import ServerControl

        await self._lock_then_leak()
        before = await db.lockdown_rows(self.guild.id)

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction)

        self.assertNotIn("Repaired", self._said())
        self.assertTrue(self.channel.overwrites[self.normal].pair()[0].send_messages)
        self.assertEqual(await db.lockdown_rows(self.guild.id), before)

    async def test_a_channel_opened_by_hand_is_locked_again(self):
        from cogs.server_control import ServerControl

        await self._lock()
        # Somebody opened it by hand: the row stayed, the deny did not.
        self.channel.overwrites[self.guild.default_role] = Overwrite()

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction, True)

        _allow, deny = self.channel.overwrites[self.guild.default_role].pair()
        self.assertTrue(deny.send_messages)
        said = self._said()
        self.assertIn("deny is gone", said)
        self.assertIn("Repaired", said)

    async def test_a_clean_server_gets_a_green_card_and_no_repair_sentence(self):
        from cogs.server_control import ServerControl

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction)

        said = self._said()
        self.assertIn("No holes found", said)
        self.assertNotIn("Repaired", said)

    def test_the_audit_is_declared_admin_only_and_always_reachable(self):
        from utils import features, permissions

        self.assertEqual(permissions.COMMAND_ACCESS["check"], permissions.ADMIN)
        self.assertIn("check", features.ALWAYS_ALLOWED_COMMANDS)
        self.assertIn("check", features.ROOT_COMMANDS)


class _Response:
    """What a deferred interaction's response looks like to `send_card`."""

    def __init__(self):
        self.messages = []

    def is_done(self):
        return False

    async def defer(self, **_payload):
        return None

    async def send_message(self, **payload):
        self.messages.append(payload)


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, **payload):
        self.messages.append(payload)


class _FakeCheck:
    """Just enough of an interaction to drive `/check`'s real callback."""

    def __init__(self, guild, channel, *, user_id=42, name="R A J"):
        self.guild = guild
        self.guild_id = guild.id
        self.channel = channel
        self.user = SimpleNamespace(
            id=user_id, display_name=name, name=name.lower(), mention=f"<@{user_id}>"
        )
        self.response = _Response()
        self.followup = _Followup()

    async def edit_original_response(self, **_payload):
        return None


def _payload_text(payload) -> str:
    """Everything a card payload says, whether it went out as a layout or an embed."""
    view = payload.get("view")
    if view is not None and hasattr(view, "to_components"):
        parts: list = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("content"), str):
                    parts.append(node["content"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(view.to_components())
        return " ".join(parts)
    embed = payload.get("embed")
    if embed is not None:
        return f"{getattr(embed, 'title', '')} {getattr(embed, 'description', '')}"
    return str(payload.get("content") or "")


if __name__ == "__main__":
    unittest.main()
