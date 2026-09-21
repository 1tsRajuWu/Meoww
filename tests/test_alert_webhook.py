"""The alerts that have to arrive when the ordinary path cannot be used.

Three situations, one transport. A server that has *just* added the bot is in
none of that server's channels; a down gateway is precisely why the web process
is answering; and a flood arrives at the web surface, which has no gateway at all.
An operator channel cannot receive any of those, so this is a webhook — silent,
bounded, and off unless configured.

The tests that matter most here are not about HTTP. They are about **what
leaves the deployment**: the module doubles as the field list that PRIVACY.md
section 5 and TERMS.md section 12 describe, so an entry that grows a new key is a
policy change and has to fail the suite rather than ship quietly.
"""
import asyncio
import json
import unittest
from unittest import mock

import bot as bot_module
from utils import alert_webhook, config_audit


class _Response:
    """The bit of an ``http.client`` response the poster reads."""

    def __init__(self):
        self.read_bytes = b""

    def read(self, _size=1):
        return self.read_bytes

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeUrlopen:
    """Records every POST, and can be made to fail on demand."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, request, timeout=None):
        if self.fail:
            raise OSError("webhook unreachable")
        self.calls.append(request)
        return _Response()


class AlertWebhook(unittest.TestCase):
    """The transport, the bounds, and the promise that it never breaks a caller."""

    def setUp(self):
        alert_webhook.reset()

    def tearDown(self):
        alert_webhook.reset()

    def _configured(self, url="https://discord.test/api/webhooks/1/abc"):
        return mock.patch.object(config_audit, "get", lambda key: url)

    def _unset(self):
        return mock.patch.object(config_audit, "get", lambda key: "")

    def test_it_is_off_until_configured_and_says_so(self):
        """\"We would have told you, if it had been switched on\" is the failure
        mode this repository keeps being caught by, so an unset URL is reported."""
        with self._unset():
            self.assertFalse(alert_webhook.configured())
            entry = alert_webhook.send("test", "Something happened")
        self.assertFalse(entry["sent"])
        self.assertIn(alert_webhook.SETTING, entry["why"])
        self.assertEqual(alert_webhook.totals()["unset"], 1)

    def test_a_configured_webhook_gets_a_silent_post(self):
        poster = _FakeUrlopen()
        with self._configured(), mock.patch.object(alert_webhook.urllib.request, "urlopen", poster):
            entry = alert_webhook.send(
                "guild-joined",
                "➕ A server added the bot",
                fields=[("Server", "Hmm"), ("Members", "4")],
                severity="info",
            )
            _join_threads()
        self.assertTrue(entry["sent"])
        (request,) = poster.calls
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "https://discord.test/api/webhooks/1/abc")
        body = json.loads(request.data.decode("utf-8"))
        # SILENT, in the only place the user asked for it: this must reach the
        # channel without ringing anybody's phone.
        self.assertEqual(body["flags"] & alert_webhook.SILENT, alert_webhook.SILENT)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        embed = body["embeds"][0]
        self.assertIn("Hmm", str(embed["fields"]))
        self.assertEqual(embed["color"], 0x5A9FD4, "info is the calm strip")

    def test_a_second_event_of_the_same_kind_is_folded_not_sent(self):
        """A flood produces events by the thousand; the operator needs the start."""
        poster = _FakeUrlopen()
        with self._configured(), mock.patch.object(alert_webhook.urllib.request, "urlopen", poster):
            first = alert_webhook.send("crawler", "One")
            second = alert_webhook.send("crawler", "One, again")
            third = alert_webhook.send("honeytoken", "A different kind still goes")
            _join_threads()
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertIn("one per kind", second["why"])
        self.assertTrue(third["sent"], "the gate is per kind, not global")
        self.assertEqual(len(poster.calls), 2)
        self.assertEqual(alert_webhook.totals()["folded"], 1)

    def test_the_process_has_a_ceiling(self):
        """A deployment under attack for an hour sends a handful, not an hour."""
        poster = _FakeUrlopen()
        with self._configured(), mock.patch.object(alert_webhook.urllib.request, "urlopen", poster):
            with mock.patch.object(alert_webhook, "MAX_PER_PROCESS", 2), mock.patch.object(
                alert_webhook, "MIN_INTERVAL_SECONDS", 0.0
            ):
                results = [alert_webhook.send(f"kind-{n}", "t") for n in range(4)]
            _join_threads()
        self.assertEqual([r["sent"] for r in results], [True, True, False, False])
        self.assertIn("already sent", results[-1]["why"])
        self.assertEqual(alert_webhook.totals()["capped"], 2)

    def test_a_dead_webhook_never_raises_and_is_recorded_as_failed(self):
        poster = _FakeUrlopen(fail=True)
        with self._configured(), mock.patch.object(alert_webhook.urllib.request, "urlopen", poster):
            entry = alert_webhook.send("guild-joined", "A server added the bot")
            _join_threads()
        self.assertFalse(entry["sent"], "a promise that was not kept is not a send")
        self.assertIn("did not answer", entry["why"])
        self.assertEqual(alert_webhook.totals()["failed"], 1)

    def test_the_payload_carries_nothing_the_policy_does_not_list(self):
        """The entry *is* the field list the legal documents describe.

        A new key here is a change to what leaves the deployment, so it has to fail
        this test rather than ship — and the two documents that describe it are
        named in the module docstring for whoever has to update both.
        """
        with self._unset():
            entry = alert_webhook.send("test", "t", detail="d", fields=[("A", "b")])
        self.assertEqual(
            sorted(entry),
            ["at", "detail", "kind", "sent", "severity", "title", "why"],
        )

    def test_the_state_a_page_reads_needs_no_request(self):
        with self._unset():
            alert_webhook.send("test", "t")
            state = alert_webhook.state()
        self.assertIn("configured", state)
        self.assertIn("interval_seconds", state)
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["recent"][0]["kind"], "test")


class TheFieldList(unittest.TestCase):
    """What a security event looks like when it is forwarded.

    The guard records an address, an agent and a path; this is the adapter that
    decides how much of that travels. It lives beside the paragraph in the module
    docstring rather than in the Flask hook so the two cannot drift.
    """

    def setUp(self):
        alert_webhook.reset()

    def tearDown(self):
        alert_webhook.reset()

    def test_a_trap_is_an_alert_and_a_crawler_is_a_warning(self):
        event = {
            "kind": "honeytoken", "agent": "GPTBot/1.0", "path": "/private",
            "ip": "203.0.113.7", "country": "SG", "hits": 3,
        }
        with mock.patch.object(alert_webhook, "send") as send:
            alert_webhook.guard_event("honeytoken", event, label="meow-bot.pages.dev")
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["severity"], "alert")
        names = [name for name, _ in kwargs["fields"]]
        self.assertEqual(
            names, ["Address", "Country", "Client", "Path", "Times", "Where"]
        )
        self.assertIn("GPTBot", str(kwargs["fields"][2][1]))

    def test_a_refusal_without_an_address_still_reads(self):
        event = {"kind": "crawler", "agent": "", "path": "", "ip": "", "country": "", "hits": 12}
        with mock.patch.object(alert_webhook, "send") as send:
            alert_webhook.guard_event("crawler", event)
        fields = dict(send.call_args.kwargs["fields"])
        self.assertEqual(fields["Address"], "not reported")
        self.assertEqual(fields["Client"], "no user-agent")
        self.assertEqual(fields["Times"], "12")
        self.assertNotIn("Where", fields, "no label, no field")


class TheJoinAlerts(unittest.TestCase):
    """The two events the operator asked for by name: a server, and a person."""

    def setUp(self):
        alert_webhook.reset()

    def tearDown(self):
        alert_webhook.reset()

    def test_the_card_names_the_inviter_and_how_old_that_account_is(self):
        guild = mock.Mock()
        guild.name = "Hmm"
        guild.id = 777
        guild.member_count = 4
        guild.owner = "RAJ"
        guild.me = mock.Mock()
        guild.me.id = 999
        fields = dict(
            bot_module._join_fields(
                guild,
                {"name": "RAJ#1", "id": 1234, "created": _minutes_ago(3)},
            )
        )
        self.assertEqual(fields["Server"], "Hmm")
        self.assertEqual(fields["Id"], "777")
        self.assertIn("RAJ#1", fields["Added by"])
        self.assertEqual(fields["Their account"], "3 minutes old")

    def test_an_unreadable_inviter_says_which_permission_is_missing(self):
        """`None` is a fact about permissions, not a blank — so it is a sentence."""
        guild = mock.Mock()
        guild.name = "Hmm"
        guild.id = 777
        guild.member_count = None
        guild.owner = None
        guild.owner_id = 5
        fields = dict(bot_module._join_fields(guild, None))
        self.assertIn("View Audit Log", fields["Added by"])
        self.assertEqual(fields["Members"], "not reported")
        self.assertEqual(fields["Owner"], "id 5")

    def test_account_age_is_coarse_on_purpose(self):
        self.assertEqual(bot_module._age_text(_minutes_ago(3)), "3 minutes old")
        self.assertEqual(bot_module._age_text(_minutes_ago(60 * 26)), "1 day old")
        self.assertEqual(bot_module._age_text(_minutes_ago(60 * 24 * 9)), "9 days old")
        self.assertEqual(bot_module._age_text(None), "age not reported")

    def test_a_member_joining_is_reported_out_of_band(self):
        """The one event the operator asked for by name, on the join path itself."""
        member = mock.Mock()
        member.guild.name = "Hmm"
        member.guild.id = 777
        member.guild.member_count = 4
        member.created_at = _minutes_ago(2)

        with mock.patch.object(alert_webhook, "send") as send, mock.patch.object(
            bot_module.db, "get_setting", mock.AsyncMock(return_value=None)
        ):
            asyncio.run(bot_module.on_member_join(member))
        kind, _title = send.call_args[0]
        self.assertEqual(kind, "member-joined")
        fields = dict(send.call_args.kwargs["fields"])
        self.assertEqual(fields["Server"], "Hmm")
        self.assertEqual(fields["Account"], "2 minutes old")
        # The person is never named. A join alert is about the pattern.
        self.assertNotIn(str(member), str(fields))

    def test_a_join_that_cannot_be_reported_does_not_fail_the_join(self):
        """The default-role work still has to happen when the webhook is down."""
        member = mock.Mock()
        member.guild.name = "Hmm"
        member.guild.id = 777
        member.guild.member_count = 4
        member.created_at = _minutes_ago(2)
        seen = []

        async def setting(_guild_id, key):
            seen.append(key)
            return None

        with mock.patch.object(
            alert_webhook, "send", side_effect=RuntimeError("no route")
        ), mock.patch.object(bot_module.db, "get_setting", setting):
            asyncio.run(bot_module.on_member_join(member))
        self.assertEqual(seen, ["default_role_id"], "the handler carried on")

    def test_the_setting_is_declared_secret_so_no_value_is_ever_committed(self):
        setting = next(
            s for s in config_audit.SETTINGS if s.name == alert_webhook.SETTING
        )
        self.assertTrue(setting.secret)
        # The declaration is where an operator decides whether to go and set it, so
        # it is where the *disclosure* has to be named too — the cost of turning it
        # on is not "a webhook" but "these fields, listed in the policy".
        self.assertIn("PRIVACY", setting.what + setting.cost)


def _join_threads():
    """Wait for the poster's daemon threads, so the assertions see the calls."""
    import threading

    for thread in list(threading.enumerate()):
        if thread.name.startswith("alert-"):
            thread.join(timeout=5)


def _minutes_ago(minutes):
    import datetime

    return bot_module.discord.utils.utcnow() - datetime.timedelta(minutes=minutes)


if __name__ == "__main__":
    unittest.main()
