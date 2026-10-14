"""The operator's own door onto the Dev Cat's chat half: ``/api/devcat-simulate``.

Why the door exists is why its laws matter. The listener ignores bots and webhooks
by design, so the only way to exercise the chat half is for a person to type — which
left *"is the cat answering in there"* unanswerable from the terminal that had just
deployed it, with a silent cat indistinguishable from a switched-off one. What such
a route must therefore not get wrong is **who may open it** and **whether what it
reports is the real decision**: the report comes from the cog's own ``rehearse``,
the call the listener itself makes, so a copy of the rules cannot drift from them.
The rules' own behaviour is ``tests/test_devtoy.py``'s subject; these are the door's.

The clock laws are here rather than there for the same reason: a rehearsal that
moved a clock would spend the answer it was rehearsing, and the only way to see
that from outside is to ask twice and then ask for real.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

import keep_alive
from cogs.devtoy import DevToy
from tests.test_dashboard import _DashboardFixture
from utils import bot_channels, catfeed

PASSKEY = "secret-passkey"
DEV_GUILD = bot_channels.DEV_SERVER_GUILD_ID
OTHER_GUILD = 777001
#: The VIP application's own id, which is also the bot user's — the name a member
#: pings and the form Discord writes that ping in.
VIP_APP_ID = 1558691012975534222


def _here(coro, timeout=20.0):
    """Run one route's coroutine in this thread.

    The stand-in gateway has no event loop, so production's ``_on_bot_loop`` would
    answer "not connected" — the right answer for a booting bot, and one that has
    its own tests. This route's subject is the decision it reports, so its
    coroutine runs here instead of behind that refusal.
    """
    result = asyncio.run(coro)
    return result if isinstance(result, tuple) else (result, 200)


class TheOperatorCanAskWhatTheCatWouldSay(_DashboardFixture):
    """The route, driven the way a terminal drives it."""

    def setUp(self):
        super().setUp()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=VIP_APP_ID),
            intents=SimpleNamespace(message_content=True),
            is_module_enabled=lambda guild_id, module: True,
            add_view=lambda view: None,
            guilds=[],
        )
        self.cog = DevToy(bot)
        bot.get_cog = lambda name: self.cog if name == "DevToy" else None
        # No test in this repository may reach the network, and the brain is a
        # network call by design — the first version of this file did reach it and
        # came back `source: "model"`, which is a finding about the route and a
        # defect in the test. Stubbed to "nothing came back", which is also the
        # state a host with no key is in; the test that cares what the cat thought
        # patches it for itself.
        brain = mock.patch.object(catfeed, "say", mock.AsyncMock(return_value=None))
        brain.start()
        self.addCleanup(brain.stop)
        for patched, value in (
            ("BOT_INSTANCE", bot),
            ("_on_bot_loop", _here),
            ("ADMIN_PASSKEY", PASSKEY),
        ):
            patcher = mock.patch.object(keep_alive, patched, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, **params):
        return self.client.get(
            "/api/devcat-simulate?" + urlencode(params),
            headers={"X-Admin-Passkey": PASSKEY},
        )

    def test_the_door_is_the_passkey_and_nothing_else(self):
        self.assertEqual(self.client.get("/api/devcat-simulate?text=hello").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/devcat-simulate?text=hello", headers={"X-Admin-Passkey": "no"}
            ).status_code,
            401,
        )
        self.assertEqual(self.ask(text="hello").status_code, 200)

    def test_an_empty_message_is_refused_rather_than_rehearsed(self):
        response = self.ask()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing to try", response.get_json()["error"])

    def test_a_mention_comes_back_as_the_sentence_it_would_say(self):
        body = self.ask(text="hello cat", mentioned=1).get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["would"], "reply")
        self.assertEqual(body["matched"], "mention")
        self.assertTrue(body["reply"])
        self.assertIsNone(body["gate"])
        # With the brain stubbed to nothing — a host with no key — the sentence is
        # the written one, and the report says so rather than going quiet.
        self.assertEqual(body["source"], "written")

    def test_the_mention_is_read_out_of_the_text_when_the_caller_does_not_say(self):
        body = self.ask(text=f"hey <@{VIP_APP_ID}>").get_json()
        self.assertTrue(body["mentioned"])
        self.assertEqual(body["would"], "reply")

    def test_ids_cross_as_text(self):
        """A snowflake as a JSON number comes back as a different server."""
        body = self.ask(text="hello", mentioned=1).get_json()
        self.assertIsInstance(body["guild_id"], str)
        self.assertEqual(body["guild_id"], str(DEV_GUILD))

    def test_the_model_is_what_answers_when_there_is_one(self):
        with mock.patch.object(catfeed, "say", mock.AsyncMock(return_value="mrrp.")):
            body = self.ask(text="hello", mentioned=1).get_json()
        self.assertEqual(body["reply"], "mrrp.")
        self.assertEqual(body["source"], "model")

    def test_another_server_comes_back_with_the_gate_named(self):
        body = self.ask(text="hello", guild_id=OTHER_GUILD, mentioned=1).get_json()
        self.assertEqual(body["would"], "nothing")
        self.assertIn("one server only", body["gate"])
        self.assertIsNone(body["reply"])

    def test_a_host_without_the_cog_says_which_host_has_it(self):
        bare = SimpleNamespace(user=None, get_cog=lambda name: None)
        with mock.patch.object(keep_alive, "BOT_INSTANCE", bare):
            response = self.ask(text="hello")
        self.assertEqual(response.status_code, 409)
        self.assertIn("VIP bot only", response.get_json()["error"])

    def test_a_rehearsal_moves_no_clock(self):
        for _ in range(2):
            self.ask(text="hello", mentioned=1)
        self.assertEqual(self.cog._last_reply, {})
        self.assertEqual(self.cog._last_react, {})

    def test_json_is_accepted_the_way_the_dashboard_posts_it(self):
        response = self.client.post(
            "/api/devcat-simulate",
            json={"text": "hello there", "mentioned": True},
            headers={"X-Admin-Passkey": PASSKEY},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["would"], "reply")


if __name__ == "__main__":
    unittest.main()
