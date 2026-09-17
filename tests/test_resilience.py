"""Staying up, and telling the truth about whether it is up.

Three ways the bot could be wrong about itself, each of which happened or nearly
did:

1. An environment variable asking for a privileged intent the Developer Portal
   has not enabled makes Discord reject the *entire* connection — so a config
   slip took the whole bot offline, and the only clue was one line in a host log.
2. ``/health`` answered ``bot_ready`` from a flag set once in ``on_ready`` and
   never cleared, so a bot that had since dropped off the gateway still read as
   ready. That flag is what "is it connected?" gets answered with.
3. The error channel posted one card per failure, so a single broken command
   under load buried the channel that exists to report it.
"""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import bot as bot_module
import keep_alive
from utils import db


class _Intents:
    def __init__(self, members=False, message_content=False):
        self.members = members
        self.message_content = message_content


class _Bot:
    def __init__(self, members=False, message_content=False, ready=False, started=True):
        self.intents = _Intents(members, message_content)
        self._ready_now = ready
        self._has_started = started

    def is_ready(self):
        return self._ready_now


class _FakeResponse:
    """The one thing ``urllib`` is used for here: a JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IntentsDiscordHasNotGranted(unittest.TestCase):
    """Asking for an intent Discord refuses costs the whole connection."""

    def test_a_requested_intent_the_portal_never_granted_is_dropped(self):
        bot = _Bot(members=True, message_content=True)

        dropped = bot_module.drop_ungranted_intents(bot, (True, False))

        self.assertFalse(bot.intents.message_content, "Discord would refuse this")
        self.assertTrue(bot.intents.members, "members was granted; keep it")
        self.assertEqual(len(dropped), 1)
        self.assertIn("Message Content", dropped[0])

    def test_both_intents_are_dropped_when_neither_was_granted(self):
        bot = _Bot(members=True, message_content=True)

        dropped = bot_module.drop_ungranted_intents(bot, (False, False))

        self.assertFalse(bot.intents.members)
        self.assertFalse(bot.intents.message_content)
        self.assertEqual(len(dropped), 2)

    def test_nothing_is_dropped_when_the_portal_grants_everything(self):
        bot = _Bot(members=True, message_content=True)

        self.assertEqual(bot_module.drop_ungranted_intents(bot, (True, True)), [])
        self.assertTrue(bot.intents.message_content)

    def test_an_unanswerable_check_leaves_the_configured_intents_alone(self):
        """No answer is not the same as a refusal — ask for what was asked for."""
        bot = _Bot(members=True, message_content=True)

        self.assertEqual(bot_module.drop_ungranted_intents(bot, None), [])
        self.assertTrue(bot.intents.members)
        self.assertTrue(bot.intents.message_content)

    def test_an_intent_nobody_asked_for_is_never_added(self):
        bot = _Bot(members=False, message_content=False)

        self.assertEqual(bot_module.drop_ungranted_intents(bot, (True, True)), [])
        self.assertFalse(bot.intents.members)
        self.assertFalse(bot.intents.message_content)

    def _flags(self, flags):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse({"flags": flags})):
            return bot_module.portal_intents()

    def test_the_limited_grants_are_recognised(self):
        """An unverified app gets the *_LIMITED bit, which is every small app."""
        self.assertEqual(self._flags((1 << 15) | (1 << 17)), (True, True))
        self.assertEqual(self._flags((1 << 15)), (True, False))
        self.assertEqual(self._flags((1 << 17)), (False, True))

    def test_the_full_grants_are_recognised_too(self):
        self.assertEqual(self._flags((1 << 14) | (1 << 16)), (True, True))

    def test_unrelated_flags_are_not_mistaken_for_intents(self):
        """The commands badge sits at bit 23 and must mean nothing here."""
        self.assertEqual(self._flags(1 << 23), (False, False))
        self.assertEqual(self._flags(0), (False, False))

    def test_an_unreachable_discord_says_so_instead_of_guessing(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            self.assertIsNone(bot_module.portal_intents())


class HealthTellsTheTruthAboutTheGateway(unittest.TestCase):
    """``bot_ready`` used to stay true after the gateway went away."""

    def test_the_three_states_have_three_names(self):
        self.assertEqual(keep_alive._gateway_label(True, True), "connected")
        self.assertEqual(keep_alive._gateway_label(True, False), "reconnecting")
        self.assertEqual(keep_alive._gateway_label(False, False), "never_connected")

    def test_a_connected_bot_reports_both_answers(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", _Bot(ready=True)):
            self.assertEqual(keep_alive._gateway_state(), (True, True))

    def test_a_dropped_connection_stops_counting_as_connected(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", _Bot(ready=False)):
            started, connected = keep_alive._gateway_state()
        self.assertTrue(started, "it did log in once, which is still worth knowing")
        self.assertFalse(connected, "and it is not connected now")

    def test_no_bot_at_all_is_not_a_crash(self):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            self.assertEqual(keep_alive._gateway_state(), (False, False))

    def _health(self, bot):
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bot):
            response = keep_alive.app.test_client().get("/health")
        return response.status_code, response.get_json()

    def test_the_endpoint_keeps_the_old_field_and_adds_the_live_one(self):
        status, body = self._health(_Bot(ready=True))
        self.assertEqual(status, 200)
        self.assertIs(body["bot_ready"], True)
        self.assertEqual(body["gateway"], "connected")

    def test_the_endpoint_no_longer_claims_a_dropped_bot_is_ready(self):
        _, body = self._health(_Bot(ready=False))
        self.assertEqual(
            body["gateway"],
            "reconnecting",
            "a bot that dropped off the gateway was still answering bot_ready",
        )

    def test_the_endpoint_is_up_before_the_bot_ever_connects(self):
        _, body = self._health(_Bot(ready=False, started=False))
        self.assertEqual(body["gateway"], "never_connected")
        self.assertIs(body["bot_ready"], False)


class _SentMessage:
    def __init__(self, sink):
        self.sink = sink
        self.edits = []

    async def edit(self, **kwargs):
        # Read at edit time, so a test can break an edit after the card exists.
        if self.sink.fail_edits:
            raise RuntimeError("Unknown Message")
        self.edits.append(kwargs)
        return self


class _ErrorChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.sent = []
        self.fail_edits = False

    async def send(self, **kwargs):
        message = _SentMessage(self)
        self.sent.append((kwargs, message))
        return message


class _ErrorGuild:
    def __init__(self, gid, channels):
        self.id = gid
        self.name = "Development"
        self.text_channels = list(channels)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class _Interaction:
    def __init__(self, guild, channel, command="rank", user_id=1348622207999279146):
        self.guild = guild
        self.channel = channel
        self.user = SimpleNamespace(mention=f"<@{user_id}>", id=user_id)
        self.command = SimpleNamespace(qualified_name=command)


def _card_text(payload) -> str:
    """Everything a viewer would read on the card, however it was delivered."""
    view = payload.get("view")
    if view is not None:
        return " ".join(
            str(getattr(child, "content", "") or "") for child in view.walk_children()
        )
    embed = payload.get("embed")
    return f"{embed.title} {embed.description}" if embed is not None else ""


class OneFailureIsOneCard(unittest.IsolatedAsyncioTestCase):
    """A command that breaks on every call must not bury its own report."""

    GUILD = 1550227368889684058
    CHANNEL = 1550230952028934335

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meow-errors-")
        self._original = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "errors.db")
        await db.setup()
        await db.set_setting(self.GUILD, "error_channel_id", self.CHANNEL)
        bot_module._ERROR_CARDS.clear()
        self.talk = _ErrorChannel(7001, "chat")
        self.errors = _ErrorChannel(self.CHANNEL, "bot-error")
        self.guild = _ErrorGuild(self.GUILD, [self.talk, self.errors])

    async def asyncTearDown(self):
        bot_module._ERROR_CARDS.clear()
        db.DB_PATH = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _fail(self, error, command="rank"):
        await bot_module._report_error_to_channel(
            _Interaction(self.guild, self.talk, command), error
        )

    async def test_the_first_failure_posts_a_card(self):
        await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 1)
        text = _card_text(self.errors.sent[0][0])
        self.assertIn("RuntimeError", text)
        # Where the command was run, not where the card is going.
        self.assertIn("in <#7001>", text)

    async def test_one_card_is_posted_and_the_repeat_is_counted_onto_it(self):
        await self._fail(RuntimeError("rate limited by the API"))
        await self._fail(RuntimeError("rate limited by the API"))

        self.assertEqual(len(self.errors.sent), 1, "one card, not two")
        message = self.errors.sent[0][1]
        self.assertEqual(len(message.edits), 1, "the second failure updated the card")
        text = _card_text(message.edits[0])
        self.assertIn("Seen **2** times", text)
        self.assertIn("rate limited by the API", text)

    async def test_a_different_error_still_gets_its_own_card(self):
        await self._fail(RuntimeError("boom"))
        await self._fail(ValueError("boom"))

        self.assertEqual(len(self.errors.sent), 2)

    async def test_a_different_command_still_gets_its_own_card(self):
        await self._fail(RuntimeError("boom"), command="rank")
        await self._fail(RuntimeError("boom"), command="balance")

        self.assertEqual(len(self.errors.sent), 2)

    async def test_the_count_survives_many_repeats(self):
        for _ in range(9):
            await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 1)
        message = self.errors.sent[0][1]
        self.assertEqual(len(message.edits), 8)
        self.assertIn("Seen **9** times", _card_text(message.edits[-1]))

    async def test_after_the_window_the_next_failure_posts_fresh(self):
        await self._fail(RuntimeError("boom"))
        with mock.patch.object(bot_module, "ERROR_CARD_WINDOW_SECONDS", 0):
            await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 2, "the old card is history")

    async def test_a_card_that_cannot_be_edited_is_not_counted_into(self):
        await self._fail(RuntimeError("boom"))
        self.errors.fail_edits = True
        await self._fail(RuntimeError("boom"))       # the edit fails, entry forgotten
        await self._fail(RuntimeError("boom"))       # …so this one posts again

        self.assertEqual(len(self.errors.sent), 2)
        # The dead card was forgotten, and what is tracked now is the new one —
        # never a count going into a message that is gone.
        self.assertIs(
            bot_module._ERROR_CARDS[(self.GUILD, "rank", "RuntimeError")]["message"],
            self.errors.sent[1][1],
        )

    async def test_a_server_with_no_error_channel_posts_nothing(self):
        await db.set_setting(self.GUILD, "error_channel_id", None)
        await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 0)

    async def test_a_channel_that_has_been_deleted_posts_nothing_and_does_not_raise(self):
        self.guild.text_channels = [self.talk]
        await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 0)

    async def test_reporting_a_failure_never_raises_when_discord_refuses_it(self):
        from utils import style

        with mock.patch.object(style, "send_card", side_effect=RuntimeError("Discord said no")):
            await self._fail(RuntimeError("boom"))

        self.assertEqual(len(self.errors.sent), 0)


if __name__ == "__main__":
    unittest.main()
