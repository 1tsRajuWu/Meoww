"""The voice companion: when it joins, what it says, and when it gets out.

Two halves, tested two ways. The *rules* are pure and are checked exhaustively,
because they are the part a person would argue about — who counts as alone, when
the bot leaves, what it says about a stream. The *cog* is checked against
stand-ins for a voice channel, a member and a voice client, which is the only way
to see the join/leave/move logic without a gateway.

The claims worth pinning above all:

* a channel with another bot in it is not "somebody alone" (two silent bots in one
  room is not company);
* the connection is deafened and muted — the privacy promise has to be in the
  call, not in a docstring;
* somebody who leaves a channel the bot is sitting in is not followed back in;
* a bot cannot watch a stream, and nothing in this feature pretends otherwise.
"""
import asyncio
import unittest
from unittest import mock

import discord

from tests.support import TempDatabaseMixin
from utils import company, db, media_health, style


# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #
class _VoiceState:
    def __init__(self, *, stream=False, video=False):
        self.self_stream = stream
        self.self_video = video


class _Member:
    def __init__(self, uid, name="someone", *, bot=False, stream=False, video=False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.bot = bot
        self.voice = _VoiceState(stream=stream, video=video)

    def __repr__(self):
        return self.display_name


class _Permissions:
    def __init__(self, connect=True):
        self.connect = connect


class _Channel:
    def __init__(self, cid, name, *, guild=None, members=(), connect=True, stage=False):
        self.id = cid
        self.name = name
        self.members = list(members)
        self.guild = guild
        self.connected = False
        self.connect_kwargs = None
        self.moves = []
        self._connect_allowed = connect
        self._stage = stage

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, _member):
        return _Permissions(self._connect_allowed)

    async def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.connected = True
        return _VoiceClient(self)

    async def send(self, **kwargs):
        self.sent = getattr(self, "sent", [])
        self.sent.append(kwargs)
        return self


class StageChannel(_Channel):
    """A stage, which the companion must never join.

    Named without the usual leading underscore on purpose: the module reads a
    stage's class name, so the stand-in reproduces discord.py's rather than
    politely differing from it.
    """

    type = "stage_voice"


class _VoiceClient:
    def __init__(self, channel):
        self.channel = channel
        self.disconnected = False
        self.moved = []

    async def disconnect(self, **_kwargs):
        self.disconnected = True
        self.channel.connected = False

    async def move_to(self, channel):
        self.moved.append(channel)
        self.channel = channel
        self.channel.connected = True


class _Guild:
    def __init__(self, gid=1, name="Hmm", *, channels=(), afk=None, me=None):
        self.id = gid
        self.name = name
        self.voice_channels = list(channels)
        self.stage_channels = []
        self.afk_channel = afk
        self.me = me or _Member(999, "Meow", bot=True)
        self.voice_client = None


class _Bot:
    def __init__(self, guilds=(), *, emoji=None, raises=False):
        self.guilds = list(guilds)
        self.voice_clients = []
        self.user = _Member(999, "Meow", bot=True)
        self._emoji = emoji
        self._raises = raises

    def get_emoji(self, _emoji_id):
        if self._raises:
            raise RuntimeError("no cache")
        return self._emoji

    def is_module_enabled(self, _guild_id, _module):
        return True

    async def wait_until_ready(self):
        await asyncio.Future()  # the tests drive the cog's methods directly


def _cog(bot):
    """The cog, whose reconcile loop parks on ``wait_until_ready`` for ever.

    That is deliberate: no test depends on a five-second timer, and the parked task
    is cancelled in teardown.
    """
    from cogs.company import Company

    return Company(bot)


def _said(channel) -> str:
    """Everything a card sent to this channel carries, as one searchable string.

    The card travels as a Components V2 layout, so ``str(kwargs)`` says nothing
    about it — the components are what a person would see.
    """
    out = []
    for payload in getattr(channel, "sent", []):
        view = payload.get("view") if isinstance(payload, dict) else payload
        if view is not None and hasattr(view, "to_components"):
            out.append(str(view.to_components()))
        else:
            out.append(str(payload))
    return " ".join(out)


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
class WhoIsAlone(unittest.TestCase):
    def test_one_human_and_nothing_else_is_the_trigger(self):
        self.assertTrue(company.alone([_Member(1)], bot_id=999))
        self.assertTrue(company.alone([_Member(1), _Member(999, bot=True)], bot_id=999))

    def test_two_people_are_company_for_each_other(self):
        self.assertFalse(company.alone([_Member(1), _Member(2)], bot_id=999))

    def test_another_bot_is_company_of_a_kind(self):
        """Two silent bots in one channel is not a room somebody is alone in."""
        self.assertFalse(company.alone([_Member(1), _Member(2, bot=True)], bot_id=999))

    def test_an_empty_channel_and_a_bot_only_channel_are_not_triggers(self):
        self.assertFalse(company.alone([], bot_id=999))
        self.assertFalse(company.alone(None, bot_id=999))
        self.assertFalse(company.alone([_Member(999, bot=True)], bot_id=999))

    def test_humans_in_counts_people_not_bots(self):
        channel = _Channel(1, "Lobby", members=[_Member(1), _Member(2, bot=True), _Member(999, bot=True)])
        self.assertEqual(company.humans_in(channel, bot_id=999), 1)

    def test_the_afk_channel_is_never_joined(self):
        """Discord parks people who are away there; arriving would be a joke."""
        away = _Channel(7, "AFK", members=[_Member(1)])
        self.assertFalse(company.eligible(away, bot_id=999, afk_channel_id=7))
        self.assertTrue(company.eligible(away, bot_id=999, afk_channel_id=8))

    def test_a_stage_is_never_joined(self):
        """A stage is a performance, so arriving would put the bot on stage."""
        stage = StageChannel(9, "Town Hall", members=[_Member(1)])
        self.assertTrue(company.is_stage(stage), "by its class name")
        self.assertFalse(company.eligible(stage, bot_id=999))

        # A channel that only says so in ``.type`` — which is how a real one reads
        # in the gateway, and the only reading a stand-in may bother to carry.
        by_type = _Channel(9, "Town Hall", members=[_Member(1)])
        by_type.type = "stage_voice"
        self.assertTrue(company.is_stage(by_type))
        self.assertFalse(company.eligible(by_type, bot_id=999))

        by_value = _Channel(9, "Town Hall", members=[_Member(1)])
        by_value.type = 13  # ChannelType.stage_voice
        self.assertTrue(company.is_stage(by_value))
        self.assertFalse(company.eligible(by_value, bot_id=999))

    def test_two_people_are_not_somebody_alone(self):
        room = _Channel(1, "Lobby", members=[_Member(1), _Member(2)])
        self.assertFalse(company.eligible(room, bot_id=999))


class TheDecision(unittest.TestCase):
    """One table, because every row is a case somebody would get wrong."""

    def test_connected_with_nobody_left_means_leave(self):
        self.assertEqual(company.decide(humans=0, here=True), company.LEAVE)

    def test_connected_with_real_company_means_leave(self):
        self.assertEqual(company.decide(humans=2, here=True), company.LEAVE)

    def test_connected_with_the_same_person_means_stay(self):
        self.assertEqual(company.decide(humans=1, here=True), company.STAY)

    def test_not_connected_and_nobody_alone_anywhere_means_skip(self):
        self.assertEqual(company.decide(humans=0, here=False, alone_channel=None), company.SKIP)

    def test_not_connected_and_somebody_alone_means_join(self):
        self.assertEqual(company.decide(humans=0, here=False, alone_channel="Lobby"), company.JOIN)

    def test_not_connected_and_the_room_already_has_somebody_means_skip(self):
        self.assertEqual(company.decide(humans=1, here=False, alone_channel="Lobby"), company.SKIP)


class TheSettings(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("company.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_a_server_that_never_chose_gets_auto_and_a_hello(self):
        current = await company.get(1)
        self.assertTrue(current.enabled)
        self.assertTrue(current.greeting)
        self.assertIn("sit with somebody", current.describe())

    async def test_off_is_stored_and_read_back(self):
        saved = await company.set_mode(1, "off")
        self.assertFalse(saved.enabled)
        self.assertFalse((await company.get(1)).enabled)
        self.assertIn("never join", (await company.get(1)).describe())

    async def test_silence_is_stored_and_read_back(self):
        await company.set_greeting(1, "off")
        current = await company.get(1)
        self.assertFalse(current.greeting)
        self.assertTrue(current.enabled, "saying nothing does not switch the feature off")
        self.assertIn("silently", current.describe())

    async def test_the_words_people_actually_type_are_understood(self):
        for on in ("auto", "on", "yes", "enabled", "JOIN"):
            self.assertEqual(company.parse_mode(on), company.AUTO, on)
        for off in ("off", "no", "never", "false", "disable"):
            self.assertEqual(company.parse_mode(off), company.OFF, off)
        self.assertIsNone(company.parse_mode("sometimes"))
        self.assertEqual(company.parse_greeting("silent"), False)
        self.assertEqual(company.parse_greeting("on"), True)
        self.assertIsNone(company.parse_greeting("maybe"))

    async def test_a_value_that_is_not_a_mode_changes_nothing(self):
        self.assertIsNone(await company.set_mode(1, "sometimes"))
        self.assertTrue((await company.get(1)).enabled)

    async def test_an_unreadable_setting_cannot_take_the_feature_down(self):
        with mock.patch.object(company.db, "get_setting", side_effect=RuntimeError("locked")):
            current = await company.get(1)
        self.assertTrue(current.enabled, "a database that cannot answer means the default")

    async def test_a_guild_less_call_is_the_default(self):
        self.assertTrue((await company.get(None)).enabled)
        self.assertIsNone(await company.set_mode(None, "off"))


class TheGreeting(unittest.TestCase):
    def test_a_stream_gets_the_honest_sentence(self):
        line = company.greeting(streaming=True)
        self.assertEqual(line, company.STREAM_GREETING)
        self.assertIn("can't watch", line)
        self.assertNotIn("watching your", line)

    def test_an_ordinary_arrival_says_hello_and_the_chooser_decides_which(self):
        late = company.greeting(pick=lambda options: options[-1])
        self.assertTrue(late.startswith(company.GREETINGS[-1]), late)
        first = company.greeting(pick=lambda options: options[0])
        self.assertTrue(first.startswith(company.GREETINGS[0]), first)

    def test_a_name_is_used_only_when_there_is_one(self):
        self.assertFalse(company.greeting().endswith(","))
        self.assertIn("rek_ter", company.greeting("rek_ter"))

    def test_the_field_about_a_stream_does_not_pretend_to_see_it(self):
        (name, value), = company.fields(streaming=True)
        self.assertEqual(name, "You're live")
        self.assertIn("no API for Go Live", value)
        self.assertIn("self_stream", value)

    def test_a_quiet_arrival_has_no_fields_at_all(self):
        self.assertEqual(company.fields(streaming=False), [])

    def test_the_card_body_puts_the_emoji_at_the_end(self):
        self.assertEqual(company.body("", "hello"), "hello")
        self.assertEqual(company.body("<a:p:1>", "hello"), "<a:p:1> hello")

    def test_the_card_says_how_to_turn_it_off(self):
        self.assertIn("/company mode:Off", company.footer())


class TheEmoji(unittest.TestCase):
    def test_a_tag_is_built_from_a_real_emoji(self):
        animated = type("Emoji", (), {"name": "pastelpink", "id": company.EMOJI_ID, "animated": True})()
        still = type("Emoji", (), {"name": "pastelpink", "id": company.EMOJI_ID, "animated": False})()
        self.assertEqual(company.emoji_markup(animated), f"<a:pastelpink:{company.EMOJI_ID}>")
        self.assertEqual(company.emoji_markup(still), f"<:pastelpink:{company.EMOJI_ID}>")

    def test_an_emoji_this_client_cannot_use_is_left_out_rather_than_printed(self):
        """A tag Discord will not render becomes literal text in somebody's chat."""
        self.assertEqual(company.emoji_markup(None), "")
        self.assertEqual(company.emoji_markup(type("E", (), {"name": None, "id": 1})()), "")
        self.assertEqual(company.emoji_markup(type("E", (), {"name": "x", "id": None})()), "")

    def test_resolving_uses_the_clients_own_guilds(self):
        class Client:
            def __init__(self, answer):
                self.answer = answer

            def get_emoji(self, _emoji_id):
                return self.answer

        sentinel = object()
        self.assertIs(company.resolve_emoji(Client(sentinel)), sentinel)
        self.assertIsNone(company.resolve_emoji(Client(None)))
        self.assertIsNone(company.resolve_emoji(Client(None).__class__(None)))
        self.assertIsNone(company.resolve_emoji(_Bot(raises=True)))
        self.assertIsNone(company.resolve_emoji(object()))

    def test_the_picture_falls_back_to_the_url_it_was_given(self):
        self.assertEqual(company.picture_url(None), company.EMOJI_URL)
        with_url = type("E", (), {"url": "https://cdn.example.invalid/e.gif"})()
        self.assertEqual(company.picture_url(with_url), "https://cdn.example.invalid/e.gif")

    def test_the_hardcoded_url_is_one_the_media_watch_probes(self):
        """It is a picture this bot sends, so it belongs to the weekly check."""
        self.assertTrue(media_health._is_media_url(company.EMOJI_URL), company.EMOJI_URL)
        probed = {target.url for target in media_health.scan_source()}
        self.assertIn(company.EMOJI_URL, probed)


class TheReadings(unittest.TestCase):
    """Counts and ids for the console, which is what the owner sees."""

    def _guild(self):
        lobby = _Channel(1, "Lobby", members=[_Member(7, "rek_ter", stream=True)])
        pair = _Channel(2, "Music", members=[_Member(8), _Member(9)])
        solo_video = _Channel(3, "Gaming", members=[_Member(10, video=True)])
        return _Guild(channels=[lobby, pair, solo_video])

    def test_alone_rows_name_the_channel_and_the_person_by_id(self):
        rows = company.alone_rows(self._guild(), bot_id=999)
        self.assertEqual([row["channel"] for row in rows], ["Lobby", "Gaming"])
        self.assertEqual(rows[0]["user_id"], "7")
        self.assertTrue(rows[0]["streaming"])
        self.assertTrue(rows[1]["video"])

    def test_alone_rows_skip_a_channel_with_two_people(self):
        guild = _Guild(channels=[_Channel(2, "Music", members=[_Member(8), _Member(9)])])
        self.assertEqual(company.alone_rows(guild, bot_id=999), [])

    def test_stream_rows_list_everybody_live_or_on_camera(self):
        rows = company.stream_rows(self._guild())
        self.assertEqual({row["user_id"] for row in rows}, {"7", "10"})
        self.assertEqual(rows[0]["channel"], "Lobby")


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #
class _CogCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = None

    def tearDown(self):
        if self.cog is not None:
            self.cog.reconcile.cancel()

    async def _make(self, guild, *, emoji=None):
        bot = _Bot([guild], emoji=emoji)
        self.cog = _cog(bot)
        return bot

    def _solo(self, guild, **member_kwargs):
        member = _Member(7, "rek_ter", **member_kwargs)
        channel = _Channel(1, "Lobby", guild=guild, members=[member])
        return channel, member


class Arriving(_CogCase):
    async def test_it_joins_deafened_and_muted(self):
        guild = _Guild()
        channel, _member = self._solo(guild)
        guild.voice_channels = [channel]
        await self._make(guild)
        self.assertTrue(await self.cog._arrive(channel, company.Settings()))
        self.assertEqual(channel.connect_kwargs, {"self_deaf": True, "self_mute": True, "timeout": 15})

    async def test_the_promise_is_in_the_call_not_the_docstring(self):
        """Deafened *and* muted: it can neither hear the room nor speak into it."""
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        await self._make(guild)
        await self.cog._arrive(channel, company.Settings())
        for flag in ("self_deaf", "self_mute"):
            self.assertIs(channel.connect_kwargs[flag], True, flag)

    async def test_it_says_hello_in_the_channels_own_text_chat(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        await self._make(guild)
        await self.cog._arrive(channel, company.Settings())
        self.assertTrue(getattr(channel, "sent", []), "nothing was said on arrival")
        sent = _said(channel)
        self.assertIn("You're not alone", sent)
        self.assertIn(company.EMOJI_URL, sent, "the picture travels even without the tag")

    async def test_a_stream_is_announced_as_a_fact_and_not_as_a_watch(self):
        guild = _Guild()
        channel, _ = self._solo(guild, stream=True)
        await self._make(guild)
        await self.cog._arrive(channel, company.Settings())
        sent = _said(channel)
        self.assertIn("You're live", sent)
        self.assertIn("no API for Go Live", sent)

    async def test_a_server_that_asked_for_silence_gets_silence(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        await self._make(guild)
        await self.cog._arrive(channel, company.Settings(greeting=False))
        self.assertEqual(getattr(channel, "sent", []), [])

    async def test_a_useless_tag_is_not_printed(self):
        """The deployment is not in every server the emoji came from."""
        guild = _Guild()
        channel, _ = self._solo(guild)
        await self._make(guild)  # get_emoji answers None
        await self.cog._arrive(channel, company.Settings())
        sent = _said(channel)
        self.assertNotIn("pastelpink", sent)
        self.assertNotIn(f":{company.EMOJI_ID}>", sent)

    async def test_a_usable_emoji_is_carried_as_its_tag(self):
        emoji = type("E", (), {"name": "pastelpink", "id": company.EMOJI_ID, "animated": True,
                              "url": company.EMOJI_URL})()
        guild = _Guild()
        channel, _ = self._solo(guild)
        await self._make(guild, emoji=emoji)
        await self.cog._arrive(channel, company.Settings())
        self.assertIn(f"<a:pastelpink:{company.EMOJI_ID}>", _said(channel))

    async def test_a_channel_it_cannot_connect_to_is_refused_before_trying(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        channel._connect_allowed = False
        await self._make(guild)
        self.assertFalse(await self.cog._arrive(channel, company.Settings()))
        self.assertIsNone(channel.connect_kwargs, "the handshake should not have been attempted")
        self.assertGreater(self.cog._blocked_until, 0)

    async def test_a_failed_connection_backs_off_instead_of_hammering(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        await self._make(guild)

        async def boom(**_kwargs):
            raise discord.ClientException("voice handshake failed")

        channel.connect = boom
        self.assertFalse(await self.cog._arrive(channel, company.Settings()))
        before = self.cog._blocked_until
        await self.cog._reconcile_once()
        self.assertEqual(self.cog._blocked_until, before, "a backoff must hold")
        self.assertIsNone(getattr(channel, "connect_kwargs", None))

    async def test_it_waits_for_the_grace_period(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        await self._make(guild)
        import time

        self.cog._note(channel, time.time())
        self.assertIsNone(self.cog._pick(guild, time.time()), "arrived too early")
        later = time.time() + company.GRACE_SECONDS + 1
        self.assertIs(self.cog._pick(guild, later), channel)

    async def test_somebody_who_left_is_not_followed_back_in(self):
        import time

        guild = _Guild()
        channel, member = self._solo(guild)
        guild.voice_channels = [channel]
        await self._make(guild)
        now = time.time()
        self.cog._alone_since[channel.id] = now - company.GRACE_SECONDS - 1
        self.cog._left_us[member.id] = now
        self.assertIsNone(self.cog._pick(guild, now), "the cooldown has to hold")
        self.assertIs(
            self.cog._pick(guild, now + company.COOLDOWN_SECONDS + 1), channel,
            "after the cooldown, and only if they stayed",
        )

    async def test_it_never_opens_a_second_connection(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        bot = await self._make(guild)
        bot.voice_clients.append(_VoiceClient(_Channel(5, "Elsewhere")))
        await self.cog._reconcile_once()
        self.assertIsNone(channel.connect_kwargs)

    async def test_a_module_that_is_off_means_no_arrival(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        bot = await self._make(guild)
        bot.is_module_enabled = lambda _guild_id, _module: False
        import time

        self.cog._alone_since[channel.id] = time.time() - company.GRACE_SECONDS - 1
        await self.cog._reconcile_once()
        self.assertIsNone(channel.connect_kwargs)

    async def test_the_setting_off_means_no_arrival(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        guild.voice_channels = [channel]
        with mock.patch.object(company, "get", new=_settings_off):
            await self._make(guild)
            import time

            self.cog._alone_since[channel.id] = time.time() - company.GRACE_SECONDS - 1
            await self.cog._reconcile_once()
        self.assertIsNone(channel.connect_kwargs)


async def _settings_off(_guild_id):
    return company.Settings(mode=company.OFF)


class Leaving(_CogCase):
    async def test_it_leaves_when_the_room_empties(self):
        guild = _Guild()
        channel, member = self._solo(guild)
        client = _VoiceClient(channel)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        channel.members = []  # they left
        await self.cog._leave_where_the_company_is_gone()
        self.assertTrue(client.disconnected)

    async def test_a_second_human_is_what_it_was_standing_in_for(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        client = _VoiceClient(channel)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        channel.members.append(_Member(8, "friend"))
        await self.cog._leave_where_the_company_is_gone()
        self.assertTrue(client.disconnected)

    async def test_it_stays_while_the_person_is_still_alone(self):
        guild = _Guild()
        channel, member = self._solo(guild)
        client = _VoiceClient(channel)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        channel.members.append(_Member(999, "Meow", bot=True))
        await self.cog._leave_where_the_company_is_gone()
        self.assertFalse(client.disconnected)

    async def test_turning_the_module_off_evicts_it(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        client = _VoiceClient(channel)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        bot.is_module_enabled = lambda _guild_id, _module: False
        await self.cog._reconcile_once()
        self.assertTrue(client.disconnected)

    async def test_a_failed_disconnect_is_reported_not_raised(self):
        guild = _Guild()
        channel, _ = self._solo(guild)
        client = _VoiceClient(channel)

        async def boom(**_kwargs):
            raise RuntimeError("socket gone")

        client.disconnect = boom
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        channel.members = []
        await self.cog._leave_where_the_company_is_gone()  # must not raise
        self.assertFalse(client.disconnected)


class Moving(_CogCase):
    async def test_a_move_to_another_empty_room_is_followed(self):
        guild = _Guild()
        lobby, _ = self._solo(guild)
        other = _Channel(2, "Clips", guild=guild, members=[_Member(7, "rek_ter")])
        client = _VoiceClient(lobby)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        await self.cog._handle_move(other)
        self.assertEqual(client.moved, [other])

    async def test_a_move_into_a_crowded_room_ends_the_visit(self):
        guild = _Guild()
        lobby, _ = self._solo(guild)
        crowded = _Channel(2, "Music", guild=guild, members=[_Member(7), _Member(8)])
        client = _VoiceClient(lobby)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        await self.cog._handle_move(crowded)
        self.assertEqual(client.moved, [])
        self.assertTrue(client.disconnected)

    async def test_leaving_voice_altogether_starts_the_cooldown(self):
        guild = _Guild()
        lobby, member = self._solo(guild)
        client = _VoiceClient(lobby)
        bot = await self._make(guild)
        bot.voice_clients.append(client)
        # Discord removes the member from the channel before it dispatches, so the
        # room is empty by the time the bot looks at it.
        lobby.members = []
        await self.cog.on_voice_state_update(
            member,
            type("S", (), {"channel": lobby})(),
            type("S", (), {"channel": None})(),
        )
        self.assertTrue(client.disconnected)
        self.assertIn(member.id, self.cog._left_us)

    async def test_a_bot_voice_event_is_ignored(self):
        guild = _Guild()
        lobby, _ = self._solo(guild)
        robot = _Member(555, "MusicBot", bot=True)
        bot = await self._make(guild)
        before = type("S", (), {"channel": None})()
        after = type("S", (), {"channel": lobby})()
        await self.cog.on_voice_state_update(robot, before, after)
        self.assertEqual(self.cog._alone_since, {}, "a bot joining is not somebody arriving")


class TheCommand(unittest.IsolatedAsyncioTestCase):
    """The command is administrator-only and says what the setting does."""

    def test_it_is_gated_by_manage_guild(self):
        from cogs.company import Company

        checks = list(getattr(Company.company_cmd, "checks", ()) or ())
        self.assertTrue(checks, "no permission check on /company")
        self.assertIn("has_permissions", " ".join(str(check) for check in checks))

    def test_it_is_declared_for_the_permission_table_and_the_registry(self):
        from utils import features, permissions

        self.assertEqual(permissions.COMMAND_ACCESS.get("company"), permissions.GUILD)
        feature = next(f for f in features.FEATURES if f.id == "company")
        self.assertEqual(feature.gate_commands, ("company",))
        self.assertEqual(feature.cog, "cogs.company")

    def test_it_has_a_help_category_of_its_own(self):
        from cogs import help as help_cog

        self.assertIn("company", help_cog.HELP_MODULES)

    def test_the_cog_the_registry_names_is_a_file_that_exists(self):
        """The registry and the tree cannot drift: this is the link between them."""
        import os

        from utils import features

        feature = next(f for f in features.FEATURES if f.id == "company")
        self.assertTrue(
            os.path.exists(f"{feature.cog.replace('.', '/')}.py"), feature.cog
        )


class TheCard(unittest.TestCase):
    def test_the_greeting_uses_the_products_own_card_layer(self):
        """The companion is a card like every other surface, scale included."""
        guild = _Guild()
        member = _Member(7, "rek_ter")
        channel = _Channel(1, "Lobby", guild=guild, members=[member])
        seen = {}

        async def capture(target, view, **_kwargs):
            seen["view"] = view
            seen["target"] = target
            return None

        async def run():
            # The cog has to be built inside the loop: starting its task needs one.
            cog = _cog(_Bot([guild]))
            try:
                with mock.patch.object(style, "send_panel", new=capture):
                    await cog._greet(channel, streaming=False)
            finally:
                cog.reconcile.cancel()

        asyncio.run(run())
        markup = str(seen["view"].to_components())
        self.assertIs(seen["target"], channel, "the room it is sitting in")
        self.assertIn("You're not alone", markup)
        self.assertIn(company.EMOJI_URL, markup)
        self.assertIn("-# ", markup, "the footer is a label on the type scale")
        self.assertIn("/company mode:Off", markup)

    def test_the_id_and_the_url_cannot_drift_apart(self):
        """The URL is a literal so the media watch can probe it; this is the tie."""
        self.assertIn(str(company.EMOJI_ID), company.EMOJI_URL)
        self.assertEqual(company.EMOJI_NAME, "pastelpink")
