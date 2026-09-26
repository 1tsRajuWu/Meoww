"""The operator console's Live page, and the watch it has to be honest about.

The voice-chat watch is the one feature in this bot that decides on every message
in a channel without anybody asking, and the console is the only place its state
is visible at all. So these tests are about the page drawing three things a
reader needs and cannot get from the source: whether it is on, *why* it is off
when it is, and the limit that no amount of code changes — Discord tells nobody
that a channel was opened.

The page is rendered through the real route with the real context, because the
defect this kind of card is prone to is not a wrong word: it is a card drawn from
a context key the route never fills in, which renders as a confidently empty
card.
"""
import unittest
from unittest import mock

from tests.test_composer import _Channel
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture
from utils import vc_watch

WATCHED = 1546716356446130228
ALERT = 1553173598158454845


class _ConsoleFixture(_DashboardFixture):
    """An operator session against the fake gateway, with no tests of its own."""

    def setUp(self):
        super().setUp()
        # The console is the operator's alone; a server-scoped sign-in is refused
        # by `test_god`'s and `test_console`'s own tests, and this fixture wants
        # the page it draws rather than the door in front of it.
        self.sign_in(scope="global")
        self.env = mock.patch.dict(
            "os.environ",
            {"VC_WATCH_CHANNEL_ID": str(WATCHED), "VC_WATCH_ALERT_CHANNEL_ID": str(ALERT)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def page(self, path="/owner/live"):
        return self.client.get(path, headers={"X-CSRF-Token": CSRF})


class TheLivePageDrawsTheWatch(_ConsoleFixture):

    def test_the_page_answers_and_carries_the_watch(self):
        body = self.page().data.decode()
        self.assertIn("The voice-chat watch", body)
        self.assertIn(str(WATCHED), body, "the watched id is printed, not just its name")
        self.assertIn(str(ALERT), body)

    def test_the_two_channels_are_named_when_the_bot_can_see_them(self):
        """The ids are settings; whether they are *channels* is a reading."""
        long_watched = _Channel(WATCHED, "commons-chat")
        long_watched.members = [object(), object()]
        self.guild._channels[WATCHED] = long_watched
        self.guild._channels[ALERT] = _Channel(ALERT, "development")
        body = self.page().data.decode()
        self.assertIn("commons-chat", body)
        self.assertIn("development", body)
        self.assertIn("In the room now", body)
        self.assertIn(
            "Discord tells nobody that a channel was",
            body,
            "the one thing this feature cannot do has to be on the card that shows it",
        )

    def test_an_alert_channel_the_bot_cannot_see_says_so_rather_than_looking_fine(self):
        self.guild._channels[WATCHED] = _Channel(WATCHED, "commons-chat")
        body = self.page().data.decode()
        self.assertIn("nothing the bot can see", body)

    def test_an_off_watch_is_drawn_as_off_and_says_which_kind_of_off(self):
        with mock.patch.dict("os.environ", {"VC_WATCH_CHANNEL_ID": "0"}):
            body = self.page().data.decode()
        self.assertIn("The watch is off", body)
        self.assertIn("off switch", body, "the reason is the half that can be acted on")

    def test_a_bot_with_no_loop_still_draws_the_page(self):
        """The console is read when something is wrong, which is when the bot is
        the least healthy it ever is — the reading may be short, never a 500."""
        self.bot.loop = None
        self.assertEqual(self.page().status_code, 200)


class TheConsoleReadsTheSameWatchTheCogRuns(unittest.TestCase):
    """One definition of "on", read by the page and the listener alike."""

    def test_the_page_and_the_listener_ask_the_same_function(self):
        self.assertIs(
            vc_watch.status.__module__,
            vc_watch.is_peek.__module__,
            "the console's reading and the decision have to come from one module",
        )
