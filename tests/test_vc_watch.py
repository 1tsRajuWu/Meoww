"""Someone reading a voice channel's chat without being in the call.

Everything here is about the *conditions*, because each one dropped makes the
feature either useless or constant: a watch that fires on the bot's own posts
reports on itself, one that fires on someone who is in the call is noise, and one
that is on in every channel in the guild is not a watch at all.

The other half is what the report says, which is tested through the listener
rather than through the card's fields: the point of the alert is that a person
reads it, so the assertions are about what lands in the channel.
"""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from discord import app_commands

from cogs import moderation
from tests.support import TempDatabaseMixin
from utils import db, modules, vc_watch

WATCHED = 424242424242424242
ALERT = 515151515151515151
GUILD = 777


class _Interaction:
    """Just enough interaction for `/watch`: a server, and somewhere to answer."""

    def __init__(self, guild_id=GUILD, guild=None):
        self.guild_id = guild_id
        self.guild = guild if guild is not None else _Guild([])
        self.response = SimpleNamespace(said=[], send_message=self._send)

    async def _send(self, content, ephemeral=False):
        self.response.said.append(content)

    @property
    def said(self) -> str:
        return "\n".join(self.response.said)


class _Member:
    def __init__(self, user_id, name="someone", bot=False, voice_channel=None, in_guild=True):
        self.id = user_id
        self.bot = bot
        self.display_name = name
        self.name = name
        self.mention = f"<@{user_id}>"
        self.display_avatar = SimpleNamespace(url=f"https://example.invalid/{user_id}.png")
        if voice_channel is None:
            self.voice = None
        else:
            self.voice = SimpleNamespace(channel=SimpleNamespace(id=voice_channel, name="Commons"))
        if in_guild:
            self.guild = SimpleNamespace(id=777)


class _Channel:
    def __init__(
        self,
        channel_id,
        name="commons-chat",
        channel_type=discord.ChannelType.voice,
        members=(),
        can_post=True,
        answers_permissions=True,
    ):
        self.id = channel_id
        self.name = name
        # The real discriminator, from the real enum: production reads
        # ``channel.type`` to tell a voice channel whose chat is readable from a
        # text channel, where nobody is ever "in" and so every message would be
        # a report.
        self.type = channel_type
        self.members = list(members)
        self.mention = f"<#{channel_id}>"
        self._can_post = can_post
        self._answers_permissions = answers_permissions
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)

    def permissions_for(self, member):
        """What Discord answers about the bot in this channel.

        ``answers_permissions=False`` is the stand-in for a reading nobody gave —
        the case ``status`` has to leave unsaid rather than report as a refusal.
        """
        if not self._answers_permissions:
            return SimpleNamespace(view_channel=True)
        return SimpleNamespace(
            view_channel=True, send_messages=self._can_post, embed_links=self._can_post
        )


class _Message:
    def __init__(self, author, channel, content="hello"):
        self.author = author
        self.channel = channel
        self.content = content
        self.attachments = []
        self.jump_url = "https://discord.com/channels/1/2/3"
        # ``None`` is what discord.py puts here for a person's message; a
        # webhook's carries the webhook's id.
        self.webhook_id = None


class _Guild:
    def __init__(self, channels):
        self.id = 777
        self._channels = {channel.id: channel for channel in channels}
        self.name = "Home"
        self.me = SimpleNamespace(id=999)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class TheDecisionIsAboutBeingInTheCall(unittest.TestCase):
    def setUp(self):
        # The two addresses are settings, so the tests set them rather than
        # depending on whatever the compiled pair happens to be.
        self.env = mock.patch.dict(
            os.environ,
            {"VC_WATCH_CHANNEL_ID": str(WATCHED), "VC_WATCH_ALERT_CHANNEL_ID": str(ALERT)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def message(self, author, channel_id=WATCHED, channel_type=discord.ChannelType.voice):
        return _Message(author, _Channel(channel_id, channel_type=channel_type))

    def test_somebody_in_another_voice_channel_is_reported(self):
        author = _Member(1, voice_channel=999)
        self.assertTrue(vc_watch.is_peek(self.message(author)))

    def test_somebody_in_no_voice_channel_at_all_is_reported(self):
        self.assertTrue(vc_watch.is_peek(self.message(_Member(1))))

    def test_somebody_in_the_call_is_not(self):
        self.assertFalse(
            vc_watch.is_peek(self.message(_Member(1, voice_channel=WATCHED))),
            "a person in the room is not reading it from outside",
        )

    def test_the_bot_and_other_bots_are_not_reported(self):
        self.assertFalse(vc_watch.is_peek(self.message(_Member(1, bot=True))))
        self.assertFalse(vc_watch.is_peek(self.message(_Member(1, bot=True, voice_channel=999))))

    def test_another_channel_is_not_the_watched_one(self):
        author = _Member(1, voice_channel=999)
        self.assertFalse(vc_watch.is_peek(self.message(author, channel_id=ALERT)))

    def test_a_direct_message_is_not_a_voice_chat(self):
        """A `User` has no voice state and no guild; nothing can be read out of one."""
        author = _Member(1, in_guild=False)
        self.assertFalse(vc_watch.is_peek(self.message(author)))

    def test_a_text_channel_is_not_a_voice_room(self):
        """Nobody is ever "in" a text channel's voice, so every message in one
        would be a report. The channel's kind is what stops that."""
        author = _Member(1, voice_channel=999)
        self.assertFalse(
            vc_watch.is_peek(self.message(author, channel_type=discord.ChannelType.text))
        )

    def test_a_webhook_post_is_not_a_person_reading_the_room(self):
        """A webhook is never in the call, and it is not somebody eavesdropping
        either — every automated post into that chat would be a report."""
        message = self.message(_Member(1, voice_channel=999))
        message.webhook_id = 1234567890
        self.assertFalse(vc_watch.is_peek(message))

    def test_the_watch_is_off_when_both_addresses_are_the_same_channel(self):
        with mock.patch.dict(os.environ, {"VC_WATCH_ALERT_CHANNEL_ID": str(WATCHED)}):
            self.assertFalse(vc_watch.watching())
            self.assertFalse(vc_watch.is_peek(self.message(_Member(1, voice_channel=999))))

    def test_the_addresses_are_settings(self):
        with mock.patch.dict(os.environ, {"VC_WATCH_CHANNEL_ID": "12345"}):
            self.assertEqual(vc_watch.watched_channel_id(), 12345)
        # A typo falls back to the compiled pair rather than raising: this is
        # read on the message path, and it must not be the reason a listener
        # errors on every message in the server.
        with mock.patch.dict(os.environ, {"VC_WATCH_CHANNEL_ID": "not-a-number"}):
            self.assertEqual(vc_watch.watched_channel_id(), vc_watch._DEFAULT_WATCHED_CHANNEL_ID)


class TheConsoleCanReadTheWatch(unittest.TestCase):
    """``status`` — the reading the operator console draws.

    A watch that is off is the thing an operator most needs told: it is the one
    feature in this bot that runs on every server without anybody asking, so
    "off" and "on and pointed at a channel the bot cannot post in" both look
    exactly like a working watch until the day a report was needed.
    """

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {"VC_WATCH_CHANNEL_ID": str(WATCHED), "VC_WATCH_ALERT_CHANNEL_ID": str(ALERT)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def guild(self, **kwargs):
        watched = _Channel(WATCHED, "commons-chat", members=[object(), object()])
        alert = _Channel(ALERT, "development", channel_type=discord.ChannelType.text, **kwargs)
        return _Guild([watched, alert])

    def test_an_on_watch_names_both_channels_and_counts_the_room(self):
        reading = vc_watch.status(self.guild())
        self.assertTrue(reading["on"])
        self.assertEqual(reading["why"], "")
        self.assertTrue(reading["resolvable"])
        self.assertEqual(reading["watched"]["name"], "commons-chat")
        self.assertEqual(reading["watched"]["people"], 2, "the room's own count, not a guess")
        self.assertEqual(reading["alert"]["name"], "development")
        self.assertTrue(reading["postable"], "the fixture's bot can post there")

    def test_a_bot_that_cannot_post_in_the_alert_channel_says_so(self):
        reading = vc_watch.status(self.guild(can_post=False))
        self.assertTrue(reading["on"], "the watch is on; the report just has nowhere to land")
        self.assertFalse(reading["postable"])

    def test_a_permission_nobody_answered_is_left_unsaid(self):
        reading = vc_watch.status(self.guild(answers_permissions=False))
        self.assertIsNone(reading["postable"])

    def test_the_same_channel_in_both_fields_says_why_the_watch_is_off(self):
        with mock.patch.dict(os.environ, {"VC_WATCH_ALERT_CHANNEL_ID": str(WATCHED)}):
            reading = vc_watch.status(self.guild())
        self.assertFalse(reading["on"])
        self.assertIn("who noticed them", reading["why"], "the reason, not just the state")

    def test_an_address_set_to_zero_says_which_kind_of_off_this_is(self):
        """``0`` is the documented off switch; a *blank* variable is not — it falls
        back on the compiled pair, so it can never mean off."""
        with mock.patch.dict(os.environ, {"VC_WATCH_CHANNEL_ID": "0"}):
            reading = vc_watch.status(self.guild())
            self.assertFalse(reading["on"])
            self.assertIn("off switch", reading["why"])
            self.assertEqual(vc_watch.watched_channel_id(), 0)
        with mock.patch.dict(os.environ, {"VC_WATCH_CHANNEL_ID": ""}):
            self.assertEqual(vc_watch.watched_channel_id(), vc_watch._DEFAULT_WATCHED_CHANNEL_ID)
            self.assertTrue(vc_watch.status(self.guild())["on"])

    def test_without_a_guild_the_ids_are_the_reading_and_not_a_verdict(self):
        reading = vc_watch.status()
        self.assertTrue(reading["on"])
        self.assertFalse(reading["resolvable"])
        self.assertFalse(reading["watched"]["resolved"])
        self.assertIsNone(reading["postable"], "nothing was asked, so nothing is claimed")
        self.assertEqual(reading["watched"]["id"], WATCHED)

    def test_a_deployment_wide_lookup_finds_a_channel_no_server_in_hand_has(self):
        """The two addresses are deployment settings: the server an operator is
        reading is very often not the server they name.

        The server in hand here holds neither address — which is the case that
        came out of the first playtest of the console, where a watch working
        perfectly read as *nothing the bot can see* on every server but one — so
        both readings come from the deployment-wide lookup, and the one it does
        not answer for is drawn as the gap it is.
        """
        elsewhere = _Channel(WATCHED, "commons-chat", members=[object()])
        stranger = _Guild([_Channel(999, "some-other-server")])
        reading = vc_watch.status(
            stranger, find=lambda channel_id: elsewhere if channel_id == WATCHED else None
        )
        self.assertTrue(reading["watched"]["resolved"])
        self.assertEqual(reading["watched"]["name"], "commons-chat")
        self.assertEqual(reading["watched"]["people"], 1)
        # …and the alert channel, which nothing answers for, is still a gap.
        self.assertFalse(reading["alert"]["resolved"])

    def test_the_guild_in_hand_answers_when_no_deployment_lookup_was_given(self):
        """The fallback, and why it is second rather than unused.

        A caller with one server and no client to ask — the harness, a test, the
        bot before it is connected — still gets an answer, and it is the server's
        own channels that answer it.
        """
        reading = vc_watch.status(self.guild())
        self.assertTrue(reading["watched"]["resolved"])
        self.assertTrue(reading["alert"]["resolved"])
        self.assertEqual(reading["alert"]["name"], "development")

    def test_an_alert_channel_the_bot_cannot_see_is_visible_as_such(self):
        guild = _Guild([_Channel(WATCHED, "commons-chat")])
        reading = vc_watch.status(guild)
        self.assertTrue(reading["resolvable"], "something *was* asked")
        self.assertFalse(reading["alert"]["resolved"], "and the alert channel is not in it")
        self.assertIsNone(reading["postable"])


class TheWatchHasItsOwnSwitch(TempDatabaseMixin, unittest.TestCase):
    """``/watch``, and the one server the watch is seeded on.

    Two defects live here. The watch used to follow *moderation's* switch — which
    is on by default in most servers — so a server that never asked for this was
    filing reports about its members into the operator's channel. And "on in my
    own server" existed only as an intention, which the dashboard would have
    drawn as *off* while the listener reported: the seed writes a real row, so
    the switch and the behaviour are the same fact.
    """

    def setUp(self):
        self.use_temp_database("watch.db")
        asyncio.run(db.setup())
        modules.invalidate()
        self.env = mock.patch.dict(
            os.environ,
            {"VC_WATCH_CHANNEL_ID": str(WATCHED), "VC_WATCH_ALERT_CHANNEL_ID": str(ALERT)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(modules.invalidate)
        self.addCleanup(self.release_temp_database)
        self.cog = moderation.Moderation(
            SimpleNamespace(
                is_module_enabled=lambda guild_id, module_id: modules.is_enabled(guild_id, module_id),
                user=SimpleNamespace(display_avatar=SimpleNamespace(url="")),
            )
        )

    def run_watch(self, state=None):
        interaction = _Interaction(guild=_Guild([_Channel(WATCHED, "commons-chat")]))
        asyncio.run(moderation.Moderation.watch.callback(self.cog, interaction, state=state))
        return interaction

    def test_the_reading_comes_before_the_write(self):
        """A server with the watch off is told so, and nothing is stored."""
        interaction = self.run_watch()
        self.assertIn("watch is off in this server", interaction.said)
        self.assertIn("/watch state:On", interaction.said)
        self.assertNotIn("watch", db.read_guild_modules_sync(GUILD))

    def test_it_writes_the_same_switch_the_listener_reads(self):
        self.run_watch(app_commands.Choice(name="On", value="on"))
        self.assertTrue(modules.is_enabled(GUILD, "watch"))
        interaction = self.run_watch(app_commands.Choice(name="Off", value="off"))
        self.assertFalse(modules.is_enabled(GUILD, "watch"))
        self.assertIn("watch is off in this server", interaction.said)

    def test_every_answer_names_both_channels(self):
        """The two addresses are deployment settings, and the card is the only
        place an administrator can read them from inside their own server."""
        said = self.run_watch().said
        self.assertIn(str(WATCHED), said)
        self.assertIn(str(ALERT), said)

    def test_the_home_server_is_on_and_a_second_boot_leaves_it_alone(self):
        self.assertTrue(vc_watch.seed_home(GUILD))
        self.assertTrue(modules.is_enabled(GUILD, "watch"))
        self.assertFalse(vc_watch.seed_home(GUILD), "the seed ran twice and rewrote the row")

    def test_a_server_that_switched_it_off_keeps_that_decision(self):
        """The row is the decision, and a boot must not overwrite one."""
        modules.set_enabled(GUILD, "watch", False)
        self.assertFalse(vc_watch.seed_home(GUILD))
        self.assertFalse(modules.is_enabled(GUILD, "watch"))


class TheReportSaysWhatHappened(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {"VC_WATCH_CHANNEL_ID": str(WATCHED), "VC_WATCH_ALERT_CHANNEL_ID": str(ALERT)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        moderation._last_peek.clear()
        moderation._peek_since.clear()
        self.alert = _Channel(ALERT, "development", discord.ChannelType.text)
        self.watched = _Channel(WATCHED, "commons-chat")
        self.guild = _Guild([self.watched, self.alert])
        # The watch has its own switch and it is off to begin with, so a report
        # test has to say that this server asked for it — the fixture turning on
        # ``moderation`` would have been the old default, which is exactly the
        # behaviour that was wrong.
        self.bot = SimpleNamespace(
            is_module_enabled=lambda guild_id, module_id: module_id == "watch",
            user=SimpleNamespace(display_avatar=SimpleNamespace(url="")),
        )
        self.cog = moderation.Moderation(self.bot)

    def deliver(self, author, content="hello", channel=None):
        message = _Message(author, channel or self.watched, content)
        message.guild = self.guild
        asyncio.run(self.cog.on_message(message))
        return message

    def test_a_peek_posts_one_card_to_the_alert_channel(self):
        self.deliver(_Member(7, name="peeper", voice_channel=999), "anyone there?")

        self.assertEqual(len(self.alert.sent), 1, "no alert was posted")
        embed = self.alert.sent[0]["embed"]
        self.assertIn("outside the call", embed.title)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("<@7>", embed.description)
        self.assertIn("anyone there?", fields["💬 What they said"])
        self.assertIn("Commons", fields["🎧 Voices"])

    def test_somebody_in_the_call_posts_nothing(self):
        self.deliver(_Member(7, voice_channel=WATCHED), "hi")
        self.assertEqual(self.alert.sent, [])

    def test_a_second_line_inside_the_quiet_window_is_counted_not_posted(self):
        """One card per person per window, with the rest counted on the next one."""
        self.deliver(_Member(7, voice_channel=999), "one")
        self.deliver(_Member(7, voice_channel=999), "two")
        self.deliver(_Member(7, voice_channel=999), "three")
        self.assertEqual(len(self.alert.sent), 1, "the quiet window let a second card through")

        # The next card counts the two it swallowed, and names the line that
        # re-opens the window rather than pretending the member went quiet.
        moderation._last_peek[(777, 7)] -= moderation._PEEK_QUIET_SECONDS + 1
        self.deliver(_Member(7, voice_channel=999), "four")
        self.assertEqual(len(self.alert.sent), 2)
        fields = {field.name: field.value for field in self.alert.sent[1]["embed"].fields}
        self.assertIn("2 more", fields["🔁 While this was quiet"])
        self.assertIn("four", fields["💬 What they said"])

    def test_a_server_that_never_asked_for_the_watch_gets_nothing(self):
        """The default, and the reason this feature has its own switch.

        Gated on ``moderation`` (which is on by default in most servers) every
        server was filing reports about its members into the operator's channel
        without anybody there having agreed to it.
        """
        self.bot.is_module_enabled = lambda guild_id, module_id: False
        self.deliver(_Member(7, voice_channel=999))
        self.assertEqual(self.alert.sent, [])

    def test_a_server_with_moderation_off_but_the_watch_on_is_still_reported(self):
        """Two systems, two switches — the reading is not a moderation power."""
        self.bot.is_module_enabled = lambda guild_id, module_id: module_id == "watch"
        self.deliver(_Member(7, name="peeper", voice_channel=999), "anyone there?")
        self.assertEqual(len(self.alert.sent), 1)

    def test_the_card_names_the_server_it_came_from(self):
        """Every report lands in one channel, including copies from servers the
        operator is not looking at, so the card has to say where it is from."""
        self.deliver(_Member(7, voice_channel=999))
        fields = {field.name: field.value for field in self.alert.sent[0]["embed"].fields}
        self.assertIn("Home", fields["🏠 Server"])
        self.assertIn(str(self.guild.id), fields["🏠 Server"])

    def test_an_alert_channel_the_bot_cannot_see_is_not_a_crash(self):
        guild = _Guild([self.watched])
        self.guild = guild
        self.deliver(_Member(7, voice_channel=999))
        self.assertEqual(self.alert.sent, [])

    def test_an_attachment_with_nothing_typed_still_says_something(self):
        message = _Message(_Member(7, voice_channel=999), self.watched, content="")
        message.guild = self.guild
        message.attachments = [object()]
        asyncio.run(self.cog.on_message(message))
        fields = {field.name: field.value for field in self.alert.sent[0]["embed"].fields}
        self.assertIn("attachment", fields["💬 What they said"])
