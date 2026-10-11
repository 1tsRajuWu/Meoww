"""What happens *after* an automatic moderation decision.

Found by walking the bot-platform rules against the tree: ``/automod`` deletes an
invite and times out a flood on its own, and until now the only trace either left
was a notice that deleted itself after ten seconds. Nobody but the member had seen
it, no moderator could answer "did the bot mute somebody?", and the member had no
way to say the bot was wrong — for a decision the *bot* made and a human never
reviewed.

So these tests are about the two things that were missing rather than the filters
themselves:

* every automatic action writes a card to the server's moderation log — the rule
  that fired, who it hit, where, and whether Discord accepted the action;
* the member it hit is told, in the notice, that ``/appeal`` exists, and that
  command files a review with a human.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import automod as automod_module
from cogs import moderation as moderation_module
from tests.support import TempDatabaseMixin
from utils import db, modlog

GUILD = 4242
LOG_CHANNEL = 900
ORIGIN = 200
MEMBER = 77


class NativeRuleDefinitions(unittest.TestCase):
    """Native rules stay bounded, named, and derived from the local settings."""

    def test_native_rules_follow_invite_and_spam_settings(self):
        payloads = automod_module.native_rule_payloads({
            "block_invites": True,
            "anti_spam": False,
            "spam_timeout_minutes": 5,
        })
        self.assertEqual(
            [payload["name"] for payload in payloads],
            [automod_module.NATIVE_INVITE_RULE, automod_module.NATIVE_SPAM_RULE],
        )
        self.assertTrue(payloads[0]["enabled"])
        self.assertFalse(payloads[1]["enabled"])
        self.assertEqual(payloads[0]["trigger"].keyword_filter, list(automod_module.NATIVE_INVITE_KEYWORDS))
        self.assertEqual(payloads[1]["trigger"].type.name, "spam")

    def test_native_timeout_is_capped_to_discords_limit(self):
        actions = automod_module.native_rule_actions(timeout_minutes=99999)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1].duration.total_seconds(), 28 * 24 * 60 * 60)


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no perms")


def _avatar():
    return SimpleNamespace(url="https://x.invalid/a.png")


class _Channel:
    def __init__(self, cid=LOG_CHANNEL, forbidden=False):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.sent = []
        self.forbidden = forbidden
        self.deleted = 0

    async def send(self, *args, **kwargs):
        if self.forbidden:
            raise _forbidden()
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=1)

    async def delete(self, *args, **kwargs):
        self.deleted += 1


class _Member:
    def __init__(self, uid=MEMBER, name="flooder"):
        self.id = uid
        self.name = name
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.display_avatar = _avatar()
        self.bot = False
        self.guild_permissions = SimpleNamespace(manage_messages=False)
        self.timeouts = []
        self.timeout_error = None

    async def timeout(self, until, reason=None):
        if self.timeout_error is not None:
            raise self.timeout_error
        self.timeouts.append((until, reason))


class _Guild:
    def __init__(self, channel):
        self.id = GUILD
        self._channel = channel
        self.owner = SimpleNamespace(
            display_name="Owner", mention="<@1>", id=1, send=_never_awaited
        )
        self.deleted_messages = []

    def get_channel(self, channel_id):
        if self._channel is not None and channel_id == self._channel.id:
            return self._channel
        return None


class _Message:
    def __init__(self, guild, channel, author, content=""):
        self.guild = guild
        self.channel = channel
        self.author = author
        self.content = content
        self.delete_error = None

    async def delete(self, *args, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.guild.deleted_messages.append(self)


def _never_awaited(*args, **kwargs):  # pragma: no cover - replaced per test
    raise AssertionError("this call was not supposed to happen")


def _card_text(payload) -> str:
    """Everything readable in a sent payload, whichever form it took.

    ``send_card`` sends the card as Components V2 and falls back to the plain
    embed when the library or Discord refuses it, so a test that only understood
    one of the two would stop noticing content the day the other path was taken.
    """
    args, kwargs = payload
    parts = [str(a) for a in args]
    for key in ("content", "embed", "embeds"):
        value = kwargs.get(key)
        if isinstance(value, discord.Embed):
            parts.append(json.dumps(value.to_dict(), default=str))
        elif isinstance(value, (list, tuple)):
            parts.append(json.dumps([e.to_dict() for e in value], default=str))
        elif value:
            parts.append(str(value))
    view = kwargs.get("view")
    if view is not None and hasattr(view, "to_components"):
        parts.append(json.dumps(view.to_components(), default=str))
    return "\n".join(parts)


class _ModlogCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("automod.db")
        # The settings table and every other one the record touches; a temp file
        # starts empty, and a missing table is a test bug rather than a finding.
        await db.setup()
        self.channel = _Channel()
        self.guild = _Guild(self.channel)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _log_to(self, channel_id):
        await db.set_setting(GUILD, modlog.SETTING, channel_id)


class TheLogDoor(_ModlogCase):
    """``utils.modlog.post`` — the one writer every case goes through."""

    async def test_a_server_that_never_ran_modlog_is_not_an_error(self):
        self.assertIsNone(await modlog.channel_for(self.guild))
        embed = discord.Embed(title="x")
        self.assertFalse(await modlog.post(self.guild, embed, what="a case"))
        self.assertEqual(self.channel.sent, [])

    async def test_a_channel_that_no_longer_exists_says_so(self):
        await self._log_to(12345)  # not this guild's channel
        with mock.patch("builtins.print") as printed:
            self.assertFalse(await modlog.post(self.guild, discord.Embed(title="x")))
        self.assertIn("[MODLOG]", printed.call_args.args[0])
        self.assertIn(str(GUILD), printed.call_args.args[0])

    async def test_a_configured_channel_receives_the_card(self):
        await self._log_to(LOG_CHANNEL)
        embed = discord.Embed(title="📋 A case", description="the whole record")
        self.assertTrue(await modlog.post(self.guild, embed, what="the case"))
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("the whole record", _card_text(self.channel.sent[0]))

    async def test_a_channel_the_bot_cannot_post_in_is_reported(self):
        await self._log_to(LOG_CHANNEL)
        self.channel.forbidden = True
        with mock.patch("builtins.print") as printed:
            self.assertFalse(await modlog.post(self.guild, discord.Embed(title="x")))
        self.assertIn("No permission", printed.call_args.args[0])


class AutomaticActionsLeaveARecord(_ModlogCase):
    """Both filters: the action happens, and a moderator can read that it did."""

    def _cog(self):
        return automod_module.AutoMod(SimpleNamespace())

    async def test_a_deleted_invite_is_logged_and_the_member_is_told_how_to_appeal(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_block_invites", True)
        cog = self._cog()
        author = _Member()
        message = _Message(self.guild, self.channel, author, "join discord.gg/example")

        await cog.on_message(message)

        self.assertIn(message, self.guild.deleted_messages)
        self.assertEqual(author.timeouts, [])
        self.assertEqual(len(self.channel.sent), 2, "a notice and one audit card")
        # The notice tells the member, briefly, what happened and where to go.
        notice = " ".join(str(call) for call in self.channel.sent[:-1])
        self.assertIn("/appeal", notice)
        self.assertNotIn("discord.gg/example", notice, "no copy of the text in the channel")
        # The card is for the humans who may have to review it.
        card = _card_text(self.channel.sent[-1])
        self.assertIn("Invite links are blocked", card)

    async def test_the_audit_card_carries_the_member_the_rule_and_the_outcome(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_block_invites", True)
        cog = self._cog()

        await cog.on_message(_Message(self.guild, self.channel, _Member(), "discord.gg/example"))

        card = _card_text(self.channel.sent[-1])
        self.assertIn(str(MEMBER), card)
        self.assertIn("Invite links are blocked", card)
        self.assertIn("Automatic action", card)

    async def test_a_flood_is_timed_out_and_logged_with_the_rule(self):
        await self._log_to(LOG_CHANNEL)
        cog = self._cog()
        author = _Member()

        for index in range(6):
            await cog.on_message(_Message(self.guild, self.channel, author, f"spam {index}"))

        self.assertEqual(len(author.timeouts), 1)
        card = _card_text(self.channel.sent[-1])
        # The rule is composed from the settings in force — the defaults here —
        # so the numbers on the card are the numbers the filter counted with.
        self.assertIn("6 or more messages in 5 seconds", card)
        self.assertIn("(`/automod antispam`)", card)
        self.assertIn("Member timed out", card)
        notice = " ".join(str(call) for call in self.channel.sent[:-1])
        self.assertIn("/appeal", notice)

    async def test_a_timeout_discord_refused_is_still_recorded_as_refused(self):
        await self._log_to(LOG_CHANNEL)
        cog = self._cog()
        author = _Member()
        author.timeout_error = _forbidden()

        for index in range(6):
            await cog.on_message(_Message(self.guild, self.channel, author, f"spam {index}"))

        self.assertEqual(author.timeouts, [])
        card = _card_text(self.channel.sent[-1])
        self.assertIn("not** muted", card)

    async def test_an_action_with_nowhere_to_log_still_acts_and_still_raises_nothing(self):
        cog = self._cog()
        author = _Member()

        for index in range(6):
            await cog.on_message(_Message(self.guild, self.channel, author, f"spam {index}"))

        self.assertEqual(len(author.timeouts), 1, "the filter still ran")


class TheAppealPath(_ModlogCase):
    """``/appeal`` — the member's door to a human."""

    def _interaction(self):
        replies = []

        async def _reply(*args, **kwargs):
            replies.append((args, kwargs))

        interaction = SimpleNamespace(
            guild=self.guild,
            guild_id=GUILD,
            user=_Member(uid=31337, name="appealing"),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock(side_effect=_reply)),
            client=SimpleNamespace(),
        )
        return interaction, replies

    def _cog(self):
        return moderation_module.Moderation(SimpleNamespace(user=SimpleNamespace(display_avatar=_avatar())))

    async def test_an_appeal_is_filed_as_a_case_in_the_log(self):
        await self._log_to(LOG_CHANNEL)
        cog = self._cog()
        interaction, replies = self._interaction()

        await moderation_module.Moderation.appeal.callback(
            cog, interaction, "I was not spamming, my connection is slow"
        )

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT case_type, user_id, reason FROM mod_cases WHERE guild_id = ?",
                (GUILD,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "Appeal")
        self.assertEqual(row[1], 31337)
        self.assertIn("connection is slow", row[2])

        card = _card_text(self.channel.sent[0])
        self.assertIn("I was not spamming", card)
        self.assertIn("/unmute", card)
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][1].get("ephemeral"))
        self.assertIn("case `#", replies[0][0][0])

    async def test_with_no_log_channel_it_reaches_the_owner_by_dm(self):
        sent = []

        async def _owner_send(*args, **kwargs):
            sent.append((args, kwargs))

        self.guild.owner = SimpleNamespace(display_name="Owner", id=1, send=_owner_send)
        cog = self._cog()
        interaction, replies = self._interaction()

        await moderation_module.Moderation.appeal.callback(cog, interaction, "please check")

        self.assertEqual(len(sent), 1, "the owner has the appeal")
        self.assertIn("please check", _card_text(sent[0]))
        self.assertIn("server owner", replies[0][0][0])

    async def test_with_nowhere_to_send_it_the_member_is_told_nothing_was_recorded(self):
        async def _refuse(*args, **kwargs):
            raise _forbidden()

        self.guild.owner = SimpleNamespace(display_name="Owner", id=1, send=_refuse)
        cog = self._cog()
        interaction, replies = self._interaction()

        await moderation_module.Moderation.appeal.callback(cog, interaction, "hello?")

        self.assertIn("/modlog", replies[0][0][0])
        self.assertIn("Nothing was recorded", replies[0][0][0])

    def test_an_appeal_carries_a_cooldown(self):
        """Spam control: one appeal per member per five minutes.

        Every other command in this cog is guarded by a *permission* check. This
        one is deliberately open to everybody, which is exactly why it needs the
        cooldown: without it, one member can flood the log a moderator has to
        read. The predicate's own name is the assertion because the cooldown is
        stored as a generated function — there is no flag to read instead.
        """
        checks = getattr(moderation_module.Moderation.appeal, "checks", [])
        self.assertTrue(
            checks and any("cooldown" in c.__qualname__ for c in checks),
            "the appeal command lost its cooldown, so one member can flood the log",
        )


class TheSourceKeepsItsPromises(unittest.TestCase):
    """The laws, checked against the handlers rather than remembered.

    A future filter added to ``on_message`` without an audit call would pass every
    behavioural test above and still be the exact bug these rules exist to
    prevent, so the file itself is read.
    """

    def _source(self) -> str:
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "cogs", "automod.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_every_automatic_branch_logs_itself(self):
        source = self._source()
        self.assertEqual(
            source.count("await modlog.post("),
            4,
            "the invite filter, the link filter, the flood timeout and the "
            "record-only flood (timeout length 0) are the four automatic actions; "
            "each has to leave a record, and a fifth filter must add one",
        )

    def test_every_notice_names_the_appeal(self):
        source = self._source()
        self.assertEqual(
            source.count("{APPEAL_POINTER}"),
            3,
            "every notice has to name /appeal — a door nobody is told about is "
            "the same as no door. Three, not four: the timeout-length-0 branch "
            "does nothing to the member, so it says nothing to them either.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class _Room:
    """A log channel, with an answer for "may the bot write here"."""

    def __init__(self, cid, can_post=True):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.can_post = can_post

    def permissions_for(self, member):
        del member
        return SimpleNamespace(send_messages=self.can_post)


class _SettingsGuild:
    def __init__(self, rooms):
        self.id = GUILD
        self._rooms = {room.id: room for room in rooms}
        self.me = object()

    def get_channel(self, channel_id):
        return self._rooms.get(channel_id)


class TheModlogCommand(_ModlogCase):
    """`/modlog` is the setting in all three of its forms, not just its setter.

    It took a channel and nothing else, so a server could not *read* where its
    log went, could not switch logging off at all, and was never told the one
    thing that silently empties an audit trail: a channel the bot may not post
    in. The bare command is the reading; ``off`` is the way out; both are here
    because an administrator who cannot see the state of their own log is being
    asked to trust it.
    """

    def _interaction(self, *rooms):
        return SimpleNamespace(guild=_SettingsGuild(list(rooms)))

    async def _run(self, interaction, **kwargs):
        sent = []

        async def _capture(_interaction, **kw):
            sent.append(kw.get("embed"))

        cog = moderation_module.Moderation.__new__(moderation_module.Moderation)
        with mock.patch.object(moderation_module, "send_card", _capture):
            await moderation_module.Moderation.modlog.callback(
                cog, interaction, **kwargs
            )
        return sent[-1]

    async def test_the_bare_command_reads_the_log_off_when_it_is_off(self):
        card = await self._run(self._interaction())

        self.assertIn("off", card.description)
        self.assertIn("/modlog #channel", card.description)

    async def test_setting_the_channel_writes_the_setting(self):
        room = _Room(LOG_CHANNEL)
        card = await self._run(self._interaction(room), channel=room)

        self.assertEqual(await db.get_setting(GUILD, modlog.SETTING), LOG_CHANNEL)
        self.assertIn(room.mention, card.description)

    async def test_a_channel_the_bot_cannot_write_in_is_said_out_loud(self):
        room = _Room(LOG_CHANNEL, can_post=False)
        card = await self._run(self._interaction(room), channel=room)

        self.assertIn("cannot post", card.description)
        self.assertIn("Send Messages", card.description)

    async def test_the_bare_command_names_the_channel_and_the_permission(self):
        room = _Room(LOG_CHANNEL)
        await db.set_setting(GUILD, modlog.SETTING, LOG_CHANNEL)

        card = await self._run(self._interaction(room))

        self.assertIn(room.mention, card.description)
        self.assertIn("can post", card.description)

    async def test_a_configured_channel_that_is_gone_is_reported_as_gone(self):
        await db.set_setting(GUILD, modlog.SETTING, 12345)

        card = await self._run(self._interaction())

        self.assertIn("no longer exists", card.description)

    async def test_off_unsets_the_row_rather_than_nulling_it(self):
        room = _Room(LOG_CHANNEL)
        await db.set_setting(GUILD, modlog.SETTING, LOG_CHANNEL)

        card = await self._run(self._interaction(room), off=True)

        self.assertIsNone(await db.get_setting(GUILD, modlog.SETTING))
        self.assertIn("off", card.description)
        self.assertIn(
            "already recorded are untouched", card.description,
            "turning logging off must not read as erasing the record",
        )


class TheShieldsAreTheServersOwn(_ModlogCase):
    """The rules the dashboard sets are the rules this listener applies.

    The cog used to hardcode one flood rule — six messages in five seconds, a
    five-minute timeout — and the card *advertised* a link filter that no code
    implemented. Both are the same defect wearing two hats: a promise the code
    cannot keep. These are the laws that the numbers below the switch are the
    numbers the filter counts with.
    """

    def _cog(self):
        return automod_module.AutoMod(SimpleNamespace())

    async def test_the_flood_rule_is_read_from_the_settings_in_force(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_spam_messages", 3)
        await db.set_setting(GUILD, "automod_spam_window_seconds", 10)
        cog = self._cog()
        author = _Member()

        for index in range(2):
            await cog.on_message(_Message(self.guild, self.channel, author, f"hi {index}"))
        self.assertEqual(author.timeouts, [], "acted before this server's own count")

        await cog.on_message(_Message(self.guild, self.channel, author, "hi 3"))
        self.assertEqual(len(author.timeouts), 1)
        card = _card_text(self.channel.sent[-1])
        self.assertIn("3 or more messages in 10 seconds", card)

    async def test_a_timeout_length_of_zero_records_the_flood_and_times_nobody_out(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_spam_timeout_minutes", 0)
        cog = self._cog()
        author = _Member()

        for index in range(6):
            await cog.on_message(_Message(self.guild, self.channel, author, f"spam {index}"))

        self.assertEqual(author.timeouts, [], "the server asked for no timeout")
        card = _card_text(self.channel.sent[-1])
        self.assertIn("Flood recorded", card)
        self.assertIn("timeout length is 0", card)
        self.assertIn("/automod status", card)

    async def test_a_blocked_link_is_deleted_and_the_member_is_told_how_to_appeal(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_block_links", True)
        cog = self._cog()
        author = _Member()
        message = _Message(self.guild, self.channel, author, "look at https://evil.example/x")

        await cog.on_message(message)

        self.assertIn(message, self.guild.deleted_messages)
        card = _card_text(self.channel.sent[-1])
        self.assertIn("Link deleted", card)
        self.assertIn("every link is blocked", card, "the rule is stated with its allowlist")
        notice = " ".join(str(call) for call in self.channel.sent[:-1])
        self.assertIn("/appeal", notice)

    async def test_an_allowlisted_domain_survives_the_link_filter(self):
        await self._log_to(LOG_CHANNEL)
        await db.set_setting(GUILD, "automod_block_links", True)
        await db.set_setting(GUILD, "automod_link_allowlist", "github.com, docs.python.org")
        cog = self._cog()
        author = _Member()

        await cog.on_message(
            _Message(self.guild, self.channel, author, "see https://gist.github.com/x")
        )
        self.assertEqual(self.guild.deleted_messages, [], "an allowed domain was blocked")

        await cog.on_message(
            _Message(self.guild, self.channel, author, "see https://evil.example/x")
        )
        self.assertEqual(len(self.guild.deleted_messages), 1)
        card = _card_text(self.channel.sent[-1])
        self.assertIn("github.com", card, "the card names what stayed allowed")

    async def test_a_lookalike_domain_is_not_the_allowed_one(self):
        """``endswith`` on the URL string would pass ``evil-github.com``."""
        self.assertFalse(automod_module.link_allowed("https://evil-github.com/x", ("github.com",)))
        self.assertTrue(automod_module.link_allowed("https://gist.github.com/x", ("github.com",)))
        self.assertTrue(automod_module.link_allowed("https://github.com", ("github.com",)))

    async def test_the_allowlist_is_read_however_it_was_typed(self):
        self.assertEqual(
            automod_module.allowed_hosts(" GitHub.com, *.example.org\nwiki.example.net; "),
            ("github.com", "example.org", "wiki.example.net"),
        )
        self.assertEqual(automod_module.allowed_hosts(None), ())

    async def test_a_bot_with_no_switch_reader_is_not_evidence_of_a_switch(self):
        """The stub-bot case: no reader means the filters run, as they always did."""
        cog = self._cog()
        self.assertTrue(cog._running(GUILD))
