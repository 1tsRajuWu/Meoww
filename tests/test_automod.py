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
        self.assertIn("Six or more messages in five seconds", card)
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
            2,
            "the invite filter and the anti-spam timeout are the two automatic "
            "actions; each has to leave a record, and a third filter must add one",
        )

    def test_every_notice_names_the_appeal(self):
        source = self._source()
        self.assertEqual(
            source.count("{APPEAL_POINTER}"),
            2,
            "both notices have to name /appeal — a door nobody is told about is "
            "the same as no door",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
