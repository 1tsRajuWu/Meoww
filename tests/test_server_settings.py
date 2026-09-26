"""The moderator log is a setting two surfaces write, and the page has to say so.

The overview's checklist tells an administrator whose server has no log channel
that moderation has "nowhere to log" and where to fix it. Driving the dashboard
found that the advice named the Server page while the Server page had no such
control at all — the reader it sent there met the lockdown button and nothing
else, which is worse than silence, because a search that ends in the wrong
button is a search that ends. So these tests are about a **write**: the picker is
on the page the finding names, the row it saves is the one the bot's own reader
resolves, and the two states the picker cannot draw as a plain option — a stored
channel Discord will not hand back, and a database that cannot be read — are
drawn as themselves rather than as "off".

The page is rendered through the real route with the real context, for the reason
``tests/test_owner_live.py`` is: a card drawn from a context key the route never
fills in renders as a confidently empty card, and no assertion about a helper
would ever see it.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.test_composer import CHANNEL_ID
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, page_href
from utils import db, modlog

OTHER_GUILD = 999002


def answer_permissions(channel, *, send=True, embed=True):
    """Make a channel's stand-in answer *both* permissions the card needs.

    The shared fake answers ``send_messages`` and nothing else, which is exactly
    the "Discord was not asked" case the code has to keep apart from a refusal —
    so a test about the refusal has to say that Discord answered, and say what.
    """
    channel.permissions_for = lambda member: SimpleNamespace(
        view_channel=True, send_messages=send, embed_links=embed
    )
    return channel


class _ServerFixture(_DashboardFixture):
    """A signed-in session on the Server page, with no tests of its own."""

    def setUp(self):
        super().setUp()
        # Every test here is about a page or a write, and both doors refuse an
        # anonymous session before they read a field — so the sign-in is the
        # fixture's rather than a line each test has to remember.
        self.sign_in()

    def page(self, path=None):
        return self.client.get(path or page_href("server"), headers={"X-CSRF-Token": CSRF})

    def stored(self):
        """The row itself, not the page's opinion of it."""
        return db.read_setting_sync(GUILD_ID, modlog.SETTING)

    def post(self, value, *, guild_id=GUILD_ID, json_door=False):
        body = {
            "csrf_token": CSRF,
            "guild_id": str(guild_id),
            "dash_return": f"{page_href('server')}?guild_id={GUILD_ID}",
            "dash_notice": "The log channel is saved.",
            "log_channel_id": "" if value is None else str(value),
        }
        if json_door:
            payload = dict(body)
            payload.pop("dash_return")
            payload.pop("dash_notice")
            return self.api("POST", "/api/server-settings", payload)
        response = self.client.post("/api/server-settings", data=body)
        if response.status_code != 302:
            return response, response.get_data(as_text=True)
        followed = self.client.get(response.headers["Location"]).get_data(as_text=True)
        return response, followed


class TheAdviceAndTheControlAreInTheSamePlace(_ServerFixture):

    def test_the_overviews_advice_points_at_a_control_that_exists(self):
        """The defect this file was written from, pinned.

        The checklist may say "on the Server page" only while the Server page
        carries the control — and it reads the finding out of the module that
        writes it rather than quoting the sentence, so rewording the fix is a
        change to the assertion too.
        """
        rows = {
            row.key: row
            for row in self.keep_alive.health_checks.findings({
                "systems": {"moderation": True},
                "log": {"configured": False},
            })
        }
        self.assertIn("log:unset", rows, "the no-log-channel case stopped being a finding")
        fix = rows["log:unset"].fix
        self.assertIn("Server page", fix)
        body = self.page().data.decode()
        self.assertIn('action="/api/server-settings"', body)
        self.assertIn('name="log_channel_id"', body)
        self.assertIn("Where the moderation record goes", body)

    def test_the_picker_offers_off_and_every_channel_the_bot_can_see(self):
        body = self.page().data.decode()
        self.assertIn("Off — cases stay in the database", body)
        self.assertIn(f'<option value="{CHANNEL_ID}"', body)
        self.assertIn("#general", body)

    def test_a_channel_the_bot_cannot_post_in_is_offered_as_such(self):
        """The permission is the picker's own reading, not a second guess at it.

        ``can_send`` comes from ``_channel_choices``, the same reading the
        composer's pickers use, so the two pages cannot disagree about which
        channels the bot can write to.
        """
        for channel in self.guild.text_channels:
            channel._can_send = False
        body = self.page().data.decode()
        self.assertIn("the bot cannot post there", body)

    def test_a_stored_channel_discord_will_not_hand_back_is_still_drawn_as_set(self):
        """A picker whose only selected option is "off" is a page telling a lie.

        A deleted channel, or a gateway that is not connected, leaves a stored id
        with no option to match it. Falling back to the first option would draw a
        server that logs as one that does not — so the stored id is drawn as
        itself, named as what it is.
        """
        gone = 424242424242
        db.write_setting_sync(GUILD_ID, modlog.SETTING, gone)
        body = self.page().data.decode()
        self.assertIn(f'<option value="{gone}" selected>', body)
        self.assertIn("not in the list Discord just gave", body)
        self.assertIn(
            '<option value="">Off — cases stay in the database</option>',
            body,
            "the off option is marked selected while a channel is stored",
        )

    def test_the_lockdown_plan_is_not_drawn_with_its_card_marks(self):
        """A sentence composed for a Discord card, printed as prose.

        Found by driving this page: the plan under "What a lockdown would do"
        read ``On **DaCat Server** that is **3** of 5 text channels`` — the
        asterisks a card would have rendered, shown literally in an HTML
        paragraph. The same strip the checklist's own rows get, at the one door.
        """
        body = self.page().data.decode()
        opened = body.index("What a lockdown would do")
        paragraph = body[opened:body.index("</p>", opened)]
        self.assertNotIn("**", paragraph)
        # The assertion is tied to the sentence the reader produces, so a plan
        # that changes wording cannot leave this test passing over a paragraph it
        # never actually found.
        plan = self.keep_alive.server_control.plan(self.guild).sentence()
        self.assertIn(
            self.keep_alive._plain_marks(plan),
            paragraph,
            "the plan is not on the page, so nothing here is being asserted about it",
        )

    def test_a_setting_that_could_not_be_read_says_so_instead_of_drawing_off(self):
        """The unread branch: no picker at all, and one sentence saying why.

        A picker drawn from a reading that failed is the page inventing the
        answer — the defect ``utils/health`` opens with, wearing a dropdown.
        """
        original = db.read_setting_sync

        def refusing(guild_id, key, default=None):
            if key == modlog.SETTING:
                raise RuntimeError("the database went away")
            return original(guild_id, key, default)

        with mock.patch.object(db, "read_setting_sync", side_effect=refusing):
            body = self.page().data.decode()
        self.assertIn("could not be read", body)
        self.assertNotIn('name="log_channel_id"', body)


class TheSaveWritesTheRowTheBotReads(_ServerFixture):

    def test_saving_a_channel_writes_the_setting_the_bot_resolves(self):
        """One constant, two doors: the page's row is ``/modlog``'s row."""
        answer_permissions(self.guild.text_channels[0])
        response, followed = self.post(CHANNEL_ID)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), CHANNEL_ID)
        self.assertIn("Moderation logs will now be sent to #general.", followed)

    def test_a_permission_nobody_answered_is_not_reported_as_a_refusal(self):
        """The shared fake's own case, and it is the one that is easy to get wrong.

        ``hasattr(perms, "embed_links")`` reads a stand-in that never heard of it
        as Discord saying no, and the operator is then told to fix a permission
        the bot already has. The page says it could not read the answer instead.
        """
        response, followed = self.post(CHANNEL_ID)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), CHANNEL_ID)
        self.assertIn("could not be read from Discord just now", followed)
        self.assertNotIn("it needs Send Messages and Embed Links", followed)

    def test_off_deletes_the_row_rather_than_storing_a_word(self):
        """An absent row is how every default in ``guild_settings`` is expressed.

        Storing ``""`` or ``0`` would read as unset through ``get_setting`` and as
        a value through anything comparing ids, which is how a feature ends up
        switching itself on again after a restart.
        """
        db.write_setting_sync(GUILD_ID, modlog.SETTING, CHANNEL_ID)
        response, followed = self.post(None)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.stored(), "the row was left behind by the off control")
        self.assertIn("Moderation logging is off", followed)

    def test_a_channel_the_bot_cannot_post_in_is_saved_and_said_out_loud(self):
        """``/modlog``'s own answer to the same case: set it, and warn.

        A permission fixed a minute later should not need the setting saved
        twice, and the sentence is the only warning a reader gets that the record
        is not going anywhere yet.
        """
        answer_permissions(self.guild.text_channels[0], send=False)
        response, followed = self.post(CHANNEL_ID)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), CHANNEL_ID)
        self.assertIn(
            "the bot cannot post there yet",
            followed,
            "the refusal has to be the endpoint's own sentence, not the picker's note",
        )

    def test_a_value_that_is_not_a_channel_id_is_refused_and_writes_nothing(self):
        db.write_setting_sync(GUILD_ID, modlog.SETTING, CHANNEL_ID)
        response, followed = self.post("the loud one")
        self.assertIn("not a channel id", followed)
        self.assertEqual(self.stored(), CHANNEL_ID, "a refused post changed the row")

    def test_the_json_door_answers_the_same_way_the_form_does(self):
        """The console and scripts post JSON; a form gets a redirect.

        Both doors run one implementation, and the JSON answer carries the
        sentence the page would have shown, so a script can log what happened.
        """
        answer_permissions(self.guild.text_channels[0])
        response = self.post(CHANNEL_ID, json_door=True)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["log_channel_id"], str(CHANNEL_ID))
        self.assertIn("#general", payload["said"])
        self.assertEqual(self.stored(), CHANNEL_ID)

    def test_a_channel_that_cannot_be_looked_up_does_not_name_a_cause(self):
        """A sentence may not pick one of the three reasons it cannot see.

        A disconnected bot, a channel somebody deleted, and a guild object with
        no lookup at all are three different things, and the first draft of this
        sentence answered all three with "the bot is not connected" — a cause
        nobody read, on the page whose whole point is not claiming what it did
        not measure.
        """
        with mock.patch.object(
            self.keep_alive, "_live_guild", return_value=SimpleNamespace(id=GUILD_ID)
        ):
            said = self.keep_alive._log_channel_sentence(GUILD_ID, CHANNEL_ID)
        self.assertIn("could not be checked just now", said)
        self.assertNotIn("not connected", said)
        self.assertIn(str(CHANNEL_ID), said, "the id is all that is known, so it is said")

    def test_a_sign_in_minted_for_one_server_cannot_set_another_servers_log(self):
        """The write door's guard, read through this route rather than assumed.

        ``_foreign_guild_refusal`` exists because a fallback that is right for a
        page silently retargets a *change*; a log channel pointed into a server
        nobody asked about is exactly the retargeting it was written to stop.
        """
        response, followed = self.post(CHANNEL_ID, guild_id=OTHER_GUILD)
        self.assertEqual(response.status_code, 302)
        self.assertIn("You do not administer that server.", followed)
        self.assertIsNone(self.stored())


if __name__ == "__main__":
    unittest.main()
