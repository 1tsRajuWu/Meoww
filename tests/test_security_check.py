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


#: Discord's own *private category* wall, the one its permission panel writes when
#: somebody turns a category off: a deny far outside anything a lockdown denies. The
#: mask on the live server this pass was written from is **39** bits; five are enough
#: to make the point and keep the law readable.
PRIVATE_CATEGORY_WALL = (
    Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"] | (1 << 4) | (1 << 29) | (1 << 0)
)


class TheRoomsACategoryOnlyThinksItHides(unittest.TestCase):
    """The deny that lives on the category and reaches none of the rooms.

    Discord's own model, and the report this class was written from: *"The Youtube
    category perms it denies, still the ice person can see and even send msg."*
    Permissions are computed **from the channel** — the category is the template a
    channel was copied from, and a room carrying an `@everyone` overwrite of its own
    is de-synced and stops taking it (Discord's *Permission Syncing*, and the reason
    its **Sync with category** button copies the category's permissions *into* the
    room). So the category draws a red X that applies to nothing, and the room is
    open exactly as the guild's own default permissions leave it.
    """

    def _rooms(self):
        """A category that denies `@everyone` View Channel, and three rooms inside it."""
        guild = Guild(guild_id=GUILD)
        youtube = Channel(10, "Youtube", kind="category")
        youtube.overwrites[guild.default_role] = Overwrite(deny=PRIVATE_CATEGORY_WALL)
        # No overwrite of its own: synced, and the category's deny *is* applied.
        synced = Channel(11, "synced")
        # The live shape: its own `@everyone` overwrite, which allows Send Messages.
        edited = Channel(12, "edited")
        edited.overwrites[guild.default_role] = Overwrite(allow=Bits.NAMES["send_messages"])
        # Carries the category's own mask by hand: hidden, and correctly so.
        masked = Channel(13, "masked")
        masked.overwrites[guild.default_role] = Overwrite(deny=PRIVATE_CATEGORY_WALL)
        guild.channels = [youtube, synced, edited, masked]
        for channel in guild.channels:
            channel.guild = guild
            channel.category = None if channel is youtube else youtube
        guild.me = _me()
        return guild, youtube, synced, edited, masked

    def test_a_room_the_category_hides_and_the_room_does_not_is_critical(self):
        guild, _youtube, _synced, edited, _masked = self._rooms()

        found = security_check.hidden_rooms(guild.channels)

        self.assertEqual(len(found), 1, "only the room with an overwrite of its own")
        self.assertEqual(found[0].level, security_check.CRITICAL)
        self.assertEqual(found[0].where, "#edited")
        self.assertIn("@everyone", found[0].detail)

    def test_a_room_with_no_overwrite_of_its_own_is_hidden_for_real(self):
        """The synced case — Discord applies the category to it, so there is no hole."""
        guild, *_rest = self._rooms()
        synced = guild.channels[1]

        self.assertEqual(security_check.hidden_rooms([synced]), [])
        self.assertFalse(security_check.hidden_by_its_category(synced))

    def test_a_room_carrying_the_category_s_own_mask_is_left_alone(self):
        """A `➕ Create A VC` given the mask by hand is hidden, and is not a finding."""
        guild, *_rest = self._rooms()
        masked = guild.channels[3]

        self.assertEqual(security_check.hidden_rooms([masked]), [])

    def test_a_category_that_hides_nothing_is_nobody_s_business(self):
        guild, youtube, _synced, edited, _masked = self._rooms()
        youtube.overwrites.clear()

        self.assertEqual(security_check.hidden_rooms([youtube, edited]), [])

    def test_the_finding_names_the_category_and_what_everyone_keeps(self):
        """The card is read beside Discord's screens: the red X, the green check, and
        nothing anywhere saying that only the room's own overwrite is applied."""
        guild, youtube, _synced, edited, _masked = self._rooms()

        found = security_check.hidden_rooms([edited])

        self.assertIn("#Youtube", found[0].title)
        self.assertIn("hidden by its category", found[0].title)
        self.assertIn("View Channels", found[0].detail)
        self.assertIn("Send Messages", found[0].detail)
        self.assertIn("Sync with category", found[0].detail)
        self.assertIn(youtube.name, found[0].title)

    def test_the_card_promises_the_undo_only_where_the_undo_will_happen(self):
        """A promise the repair keeps: this room's mark is the old unlock's own.

        The live server's nine rooms carry ``allow Send Messages, deny nothing``
        exactly — an old `/unlock`'s mark, written 27 times on one September evening
        — so `fix` can undo them by *removing* that overwrite. A card that told its
        owner to press **Sync with category** in nine places would be leaving work
        on the table the bot can do itself.
        """
        _guild, _youtube, _synced, edited, _masked = self._rooms()

        found = security_check.hidden_rooms([edited])

        self.assertIn("/check fix:True", found[0].detail)
        self.assertIn("allow Send Messages, deny nothing", found[0].detail)
        self.assertTrue(
            found[0].detail.endswith("takes its category's permissions again."),
            "the undo is not the repair the card names, so nothing promised it",
        )
        self.assertTrue(security_check._is_an_old_unlock(edited))

    def test_a_mark_a_person_made_is_offered_discord_s_repair_and_not_mine(self):
        """The fingerprint is exact, so somebody else's room is never de-synced.

        A room allowed to send **and** react by hand is not a bug's leftover, and
        removing its overwrite would be this pass taking away a permission a person
        chose. It is still a finding — the category's deny does not hold there — but
        the repair named is Discord's own.
        """
        guild, _youtube, _synced, edited, _masked = self._rooms()
        edited.overwrites[guild.default_role] = Overwrite(
            allow=Bits.NAMES["send_messages"] | Bits.NAMES["add_reactions"]
        )

        found = security_check.hidden_rooms([edited])

        self.assertIn("Sync with category", found[0].detail)
        self.assertNotIn("/check fix:True", found[0].detail)
        self.assertFalse(security_check._is_an_old_unlock(edited))

    def test_a_room_the_category_hides_is_not_also_a_visibility_note(self):
        """One hole, one finding: `visibility_notes` asks the room's own overwrite."""
        guild, _youtube, _synced, edited, _masked = self._rooms()

        self.assertEqual(security_check.visibility_notes(guild, [edited]), [])


class TheScanCarriesThem(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The finding is on the card `/check` draws, not only in a reader."""

    async def test_a_room_the_category_only_thinks_it_hides_reaches_the_card(self):
        guild, *_rest = TheRoomsACategoryOnlyThinksItHides()._rooms()

        report = await security_check.audit(guild, bot=_Bot())

        rows = [f for f in report.of(security_check.CRITICAL) if f.where == "#edited"]
        self.assertEqual(len(rows), 1)
        # The card's own sentence, so a scan that found it cannot report *no holes*
        # beside the row it found.
        self.assertIn("**1** critical", report.sentence())


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

    def test_an_old_lock_on_a_voice_channel_is_a_note_rather_than_a_repair_promise(self):
        """A voice channel is not a channel a lockdown holds — `plan()` skips it.

        The old server-wide lockdown wrote its overwrite on every channel it could,
        voice ones included, so this shape is *evidence* of one and not a lock the
        sweep can hold: the adoption pass walks text channels, which made a finding
        that said *run `/check fix:True`* a dead end an administrator could reach
        twice. It is reported as what it is, with the one command that would make it
        mine on purpose.
        """
        everyone = Role("@everyone")
        voice = Channel(
            1, "plus-vc", kind="voice",
            overwrites={everyone: Overwrite(deny=Bits.NAMES["send_messages"])},
        )
        guild = Guild(channels=[voice])
        guild.default_role = everyone

        found, beaten, loose = security_check.lockdown_findings(guild, [voice], {})

        self.assertEqual((beaten, loose), ([], []))
        self.assertEqual([f.level for f in found], [security_check.NOTE])
        self.assertIn("old lockdown", found[0].title)
        # The one command that *would* make it mine, named — and `fix` named as the
        # one that will not, which is the whole difference from the warning this
        # replaced.
        self.assertIn("`/lockdown`", found[0].detail)
        self.assertIn("leaves it alone", found[0].detail)

    def test_an_old_lock_i_cannot_manage_names_the_permission_instead_of_fix(self):
        everyone = Role("@everyone")
        old = Channel(
            1, "old", manage=False,
            overwrites={everyone: Overwrite(deny=Bits.NAMES["send_messages"])},
        )
        guild = Guild(channels=[old])
        guild.default_role = everyone

        found, _beaten, _loose = security_check.lockdown_findings(guild, [old], {})

        self.assertEqual([f.level for f in found], [security_check.WARNING])
        self.assertIn("Manage Channels", found[0].title)

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

    async def test_the_audit_names_the_old_locks_adoption_can_consider(self):
        """`fix` cannot adopt what the pass will not look at, and the card says which.

        The repair sentence is built from this list: *nothing needed repairing* beside
        a screen still full of unenforced locks is the answer that sent this server's
        administrator round twice.
        """
        guild = self._clean()
        # `@everyone` is the guild's own object: the engine reads a channel's
        # overwrite for *that* role, so a fresh look-alike would be a channel with no
        # overwrite at all and the law would pass for the wrong reason.
        old = Channel(
            9, "old",
            overwrites={
                guild.default_role: Overwrite(deny=Bits.NAMES["send_messages"])
            },
        )
        guild.channels.append(old)
        old.guild = guild

        with mock.patch.object(
            db, "get_setting", new=_settings({"log_channel_id": 5, "default_role_id": 9})
        ):
            report = await security_check.audit(guild, bot=_Bot())

        self.assertEqual([channel.name for channel in report.orphans], ["old"])
        self.assertEqual(
            [finding.title for finding in report.of(security_check.WARNING)],
            ["looks locked and is not enforced"],
        )

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
        # The caller is the server's owner: `/check` answers the owner alone, and a
        # fake that left `owner_id` unset would refuse every law below for a reason
        # none of them is about.
        self.guild.owner_id = 42
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

    async def test_the_old_unlock_is_undone_on_the_server_it_was_written_on(self):
        """The repair half, driven through the real command callback.

        The shape under test is the live one: a category that denies `@everyone`
        View Channel, and a room inside it whose own `@everyone` overwrite is
        exactly what a pre-ledger `/unlock` wrote. `fix` removes that overwrite, the
        room takes its category's permissions again, and the ledger is not touched —
        this room was never a lock, so there is no row to move.
        """
        from cogs.server_control import ServerControl

        category = Channel(70, "Youtube", kind="category")
        category.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["view_channel"]
        )
        room = Channel(71, "video-idea")
        room.overwrites[self.guild.default_role] = Overwrite(
            allow=Bits.NAMES["send_messages"]
        )
        self.guild.channels.extend([category, room])
        category.guild = self.guild
        room.guild = self.guild
        room.category = category
        before = await db.lockdown_rows(self.guild.id)

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            self.assertEqual(
                [channel.name for channel in report.strays], ["video-idea"],
                "the room the pass may undo is not the one the card names",
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        said = self._said()
        self.assertIn("Repaired", said)
        self.assertIn("put **1** room(s) back under their category", said)
        self.assertNotIn(
            self.guild.default_role, room.overwrites,
            "the old unlock's overwrite is still there, so the room is still de-synced",
        )
        self.assertIn(
            self.guild.default_role, category.overwrites,
            "the category's own deny was touched",
        )
        self.assertEqual(
            await db.lockdown_rows(self.guild.id), before,
            "undone a stray unlock by rewriting the lockdown ledger",
        )

    async def _record(self, channel, *, allow=0, deny=0, reason="a command since rewritten"):
        """Write down an overwrite as one this bot wrote, the way a writer would have."""
        await db.record_overwrites(
            self.guild.id,
            [(channel.id, self.guild.default_role.id, allow, deny, reason, 0)],
            at=1_760_000_000.0,
        )

    async def test_an_overwrite_this_bot_wrote_is_named_and_taken_back(self):
        """The record's own finding: nothing holds it, and the card says what will."""
        from cogs.server_control import ServerControl

        # A grant today's code never writes — not the old unlock's mark, which the
        # stray readers already speak for. This is the case the record exists for.
        reactions = Bits.NAMES["add_reactions"]
        self.guild.default_role.permissions = Perms(
            manage_channels=False, send_messages=True, add_reactions=True
        )
        self.channel.overwrites[self.guild.default_role] = Overwrite(allow=reactions)
        await self._record(self.channel, allow=reactions, reason="unlock by ICE")

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            rows = [
                finding
                for finding in report.findings
                if "nothing holds" in finding.title and "this bot wrote" in finding.title
            ]
            self.assertTrue(rows, "the record's own leftover was not named")
            self.assertEqual(rows[0].level, security_check.WARNING)
            self.assertIn("Add Reactions", rows[0].detail)
            self.assertIn(" on 20", rows[0].detail, "the card does not say when it was written")
            self.assertIn("UTC", rows[0].detail)
            self.assertIn("unlock by ICE", rows[0].detail)
            self.assertEqual(
                [channel.name for channel in report.recorded_grants], [self.channel.name]
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        self.assertIn("took back **1**", self._said())
        self.assertNotIn(self.guild.default_role, self.channel.overwrites)

    async def test_a_deny_this_bot_wrote_with_no_row_is_handed_to_the_ledger(self):
        """The other half: a lock nothing holds is a lock nobody can undo."""
        from cogs.server_control import ServerControl

        self.channel.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["send_messages"]
        )
        await self._record(self.channel, deny=Bits.NAMES["send_messages"], reason="lockdown by ICE")

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            self.assertEqual(
                [channel.name for channel in report.recorded_locks], [self.channel.name]
            )
            self.assertEqual(report.recorded_grants, [])
            await ServerControl.check.callback(self.cog, self.interaction, True)

        self.assertIn("wrote the ledger row for **1** lock(s)", self._said())
        self.assertTrue(
            self.channel.overwrites[self.guild.default_role].pair()[1].send_messages,
            "the deny this bot wrote was removed by its own repair",
        )

    async def test_a_room_created_with_an_overwrite_of_its_own_is_not_ours_here(self):
        """The record speaks about **changes to a room**, never a room's creation.

        A ticket channel is born denying `@everyone` `View Channel` — the shape the
        stray readers would call a hand-made hide and the record could call an
        orphan lock of ours if creation wrote rows. It does not: the deny is that
        channel's own configuration, there is nothing stale to catch and nothing to
        undo, and a row would put a warning on every ticket ever opened. This law
        pins both halves: every reader stays quiet, and `fix` changes nothing.
        """
        from cogs.server_control import ServerControl

        room = Channel(73, "ticket-0042")
        room.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["view_channel"]
        )
        self.guild.channels.append(room)
        room.guild = self.guild

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            self.assertEqual(report.recorded_locks, [], "creation was read as a lock of ours")
            self.assertEqual(report.recorded_grants, [])
            self.assertFalse(
                [
                    finding
                    for finding in report.findings
                    if "ticket-0042" in finding.where and "this bot wrote" in finding.title
                ],
                "a ticket channel's own deny was reported as a leftover of this bot's",
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        self.assertIn(
            self.guild.default_role, room.overwrites,
            "the repair touched a channel that only exists because it was created",
        )

    async def test_a_channel_the_ledger_holds_is_left_to_the_ledger(self):
        """A recorded write a lock still owns is not a leftover — `/unlock` is its undo."""
        await self._lock()

        with self._quiet_settings():
            recorded = await security_check.audit(self.guild, bot=self.bot)

        self.assertEqual(recorded.recorded_locks, [])
        self.assertEqual(recorded.recorded_grants, [])
        self.assertFalse(
            [
                finding
                for finding in recorded.findings
                if "nothing holds" in finding.title and "this bot wrote" in finding.title
            ],
            "a lock the ledger holds was reported as an orphan of the record",
        )

    async def test_the_stray_readers_speak_for_the_mark_they_own(self):
        """One room is one finding, even when two readers can both see it."""
        room = Channel(72, "general-ish")
        room.overwrites[self.guild.default_role] = Overwrite(
            allow=Bits.NAMES["send_messages"]
        )
        self.guild.channels.append(room)
        room.guild = self.guild
        await self._record(room, allow=Bits.NAMES["send_messages"])

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)

        rows = [finding for finding in report.findings if "general-ish" in finding.where]
        self.assertEqual(len(rows), 1, "the same overwrite was reported twice")
        self.assertEqual(report.recorded_grants, [], "a claimed room was listed twice")
        self.assertEqual(
            [channel.name for channel in report.strays], ["general-ish"],
            "the stray reader stopped speaking for its own mark",
        )

    async def test_the_leftover_on_a_public_room_is_named_and_taken_back(self):
        """The live `#general`: the mark with no category hiding anything — yet.

        Five rooms on the server this was written for are in exactly this state, and
        none of them is a hole *today*: they are as talkable as their category leaves
        them. What the mark costs is the future — an overwrite of its own is what
        stops a room taking its category's permissions, so the next person to hide
        `Main` watches the deny not hold, which is the report this file was written
        from. It is a note rather than a critical row, and `fix` still takes it back.
        """
        from cogs.server_control import ServerControl

        room = Channel(72, "general-ish")
        room.overwrites[self.guild.default_role] = Overwrite(
            allow=Bits.NAMES["send_messages"]
        )
        self.guild.channels.append(room)
        room.guild = self.guild

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            named = [
                finding
                for finding in report.findings
                if finding.level == security_check.NOTE and "general-ish" in finding.where
            ]
            self.assertTrue(named, "the leftover was not named at all")
            self.assertIn("never taken back", named[0].title)
            self.assertEqual(
                [channel.name for channel in report.strays], ["general-ish"]
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        self.assertIn("put **1** room(s) back under their category", self._said())
        self.assertNotIn(
            self.guild.default_role, room.overwrites,
            "the leftover is still there, so the next category change will miss it",
        )

    async def test_a_room_that_speaks_only_through_the_overwrite_keeps_it(self):
        """The guard, driven through the command: named on the card, left to a person.

        Where `@everyone`'s own role denies Send Messages, the overlay an old
        `/unlock` wrote is that room's only source of the permission — so removing it
        would silence a room rather than put one back. The room is still reported,
        and the card says the pass will not touch it and what to press instead: a
        row that promised a repair which then did not happen is the dead end this
        command was written against.
        """
        from cogs.server_control import ServerControl

        self.guild.default_role.permissions = Perms(manage_channels=False, send_messages=False)
        category = Channel(70, "Youtube", kind="category")
        category.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["view_channel"]
        )
        room = Channel(71, "video-idea")
        room.overwrites[self.guild.default_role] = Overwrite(
            allow=Bits.NAMES["send_messages"]
        )
        self.guild.channels.extend([category, room])
        category.guild = self.guild
        room.guild = self.guild
        room.category = category

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            self.assertEqual(
                report.strays, [], "a room that would go quiet was listed as undoable"
            )
            rows = [
                finding
                for finding in report.findings
                if finding.level == security_check.CRITICAL and "video-idea" in finding.where
            ]
            self.assertTrue(rows, "the hidden room stopped being a critical finding")
            self.assertIn("*only* through it", rows[0].detail)
            self.assertIn("I will not remove it", rows[0].detail)
            self.assertIn("Sync with category", rows[0].detail)
            self.assertNotIn(
                "fix:True` removes it", rows[0].detail,
                "the card promised a repair the pass will refuse to make",
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        said = self._said()
        self.assertNotIn("put **", said)
        self.assertIn(
            self.guild.default_role, room.overwrites,
            "the pass took away the only thing letting that room be spoken in",
        )

    async def test_a_person_s_own_overwrite_is_never_removed_by_the_pass(self):
        """A room hidden by its category with a *lock's* deny on it is left alone.

        It is a finding — `@everyone` can still see it — but the overwrite is not the
        bug's mark, so the pass may not remove it: what the pass undoes is one shape
        and nothing else.
        """
        from cogs.server_control import ServerControl

        category = Channel(70, "Youtube", kind="category")
        category.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["view_channel"]
        )
        room = Channel(71, "video-idea")
        room.overwrites[self.guild.default_role] = Overwrite(
            deny=Bits.NAMES["send_messages"] | Bits.NAMES["create_public_threads"]
        )
        self.guild.channels.extend([category, room])
        category.guild = self.guild
        room.guild = self.guild
        room.category = category

        with self._quiet_settings():
            report = await security_check.audit(self.guild, bot=self.bot)
            self.assertEqual(
                report.strays, [],
                "a mark the old command never wrote was listed as a stray",
            )
            self.assertIn(
                "video-idea",
                " ".join(finding.where for finding in report.findings),
                "the room stopped being a finding, so the list proves nothing",
            )
            await ServerControl.check.callback(self.cog, self.interaction, True)

        said = self._said()
        self.assertNotIn("put **", said, "the pass removed somebody else's overwrite")
        self.assertIn(self.guild.default_role, room.overwrites)
        self.assertTrue(
            room.overwrites[self.guild.default_role].pair()[1].send_messages,
            "a deny the pass had no business touching was changed",
        )

    async def test_the_reading_alone_changes_nothing(self):
        from cogs.server_control import ServerControl

        await self._lock_then_leak()
        before = await db.lockdown_rows(self.guild.id)

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction, False)

        said = self._said()
        self.assertNotIn("Repaired", said)
        # …and says that is what it was: a card that reads the same as the last one is
        # indistinguishable from a `fix` that did nothing.
        self.assertIn("Audit only", said)
        self.assertIn("fix:True", said)
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
            await ServerControl.check.callback(self.cog, self.interaction, False)

        said = self._said()
        self.assertIn("No holes found", said)
        self.assertNotIn("Repaired", said)

    async def test_old_locks_that_are_not_mine_are_named_rather_than_called_nothing(self):
        """The pass refuses what Discord says somebody else wrote — and says so.

        This is the answer that sent the report's author round twice: the card said
        *nothing needed repairing* while channels still carried an old lockdown's
        overwrite with no row, because the audit log names a person rather than the
        bot. *Nothing to do* and *nothing I may do* are not the same sentence.
        """
        from cogs.server_control import ServerControl

        old = Channel(
            9, "old",
            overwrites={self.guild.default_role: Overwrite(deny=Bits.NAMES["send_messages"])},
        )
        self.guild.channels.append(old)
        old.guild = self.guild

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, self.interaction, True)

        said = self._said()
        self.assertNotIn("Nothing needed repairing", said)
        self.assertIn("Nothing was changed", said)
        self.assertIn("old lock(s)", said)
        self.assertIn("audit log names somebody else", said)
        self.assertEqual(await db.lockdown_rows(self.guild.id), [])

    async def test_only_the_server_owner_can_run_it(self):
        """An administrator who is not the owner is refused, and nothing is written.

        `/check` reads every overwrite in a server and its `fix` rewrites them, so
        the audience is the one person Discord's own UI calls the owner — and the
        refusal has to happen before the defer, or the outsider gets the audit.
        """
        from cogs.server_control import ServerControl

        await self._lock_then_leak()
        before = await db.lockdown_rows(self.guild.id)
        outsider = _FakeCheck(self.guild, self.channel, user_id=99)

        with self._quiet_settings():
            await ServerControl.check.callback(self.cog, outsider, True)

        said = " ".join(_payload_text(payload) for payload in outsider.response.messages)
        self.assertIn("this server's owner", said)
        self.assertEqual(outsider.followup.messages, [])
        self.assertTrue(
            self.channel.overwrites[self.normal].pair()[0].send_messages,
            "an administrator who is not the owner repaired the channel",
        )
        self.assertEqual(await db.lockdown_rows(self.guild.id), before)

    def test_fix_is_a_required_choice_rather_than_a_default(self):
        """Discord must ask, because a default is what nobody noticed was never set.

        The option was an optional boolean defaulting to `False`: `/check` on its own
        audited and changed nothing, and the card that came back read exactly like the
        last one — which is the report this law is written from.
        """
        from cogs.server_control import ServerControl

        parameters = {parameter.name: parameter for parameter in ServerControl.check.parameters}
        self.assertIn("fix", parameters)
        self.assertTrue(parameters["fix"].required, "fix can still be left unset")

    def test_the_audit_is_declared_owner_only_and_always_reachable(self):
        from utils import features, permissions

        self.assertEqual(permissions.COMMAND_ACCESS["check"], permissions.OWNER)
        self.assertIn("check", features.ALWAYS_ALLOWED_COMMANDS)
        self.assertIn("check", features.ROOT_COMMANDS)


class _Response:
    """What a deferred interaction's response looks like to `send_card`.

    ``send_message`` keeps Discord's own signature — the text is positional and the
    flags are keywords — rather than the keywords-only shape that was convenient
    here: the refusal `/check` answers an outsider with is a positional sentence,
    and a stand-in that refused it would fail the law for being unlike the object
    the bot really talks to.
    """

    def __init__(self):
        self.messages = []

    def is_done(self):
        return False

    async def defer(self, **_payload):
        return None

    async def send_message(self, content=None, **payload):
        self.messages.append({**payload, "content": content})


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
