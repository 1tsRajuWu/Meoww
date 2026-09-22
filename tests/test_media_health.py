"""The media watch: what it looks at, and what it says when something is gone.

This exists because of a failure with no symptom anywhere: every roleplay card
pointed at a hand-typed ``media.tenor.com`` URL, all of them answered 404, and
what a member saw was an empty box Discord draws without complaining. So the
tests care about three things above the plumbing — that the *scan* really walks
the tree that may hold a link and skips the places that deliberately hold broken
ones, that a probe distinguishes "dead" from "could not ask", and that a clean
week is silence while a dead link is said exactly once.

The fourth thing joined later and is a different kind of finding: the **stream
path**. Every URL in the tree — every station in the catalogue included — can be
healthy on the day music stops working, because the fault is that this host cannot
open a connection at all. So one target opens a known station end to end, and the
tests keep that target honest: it is the last one, it is only one, it names the URL
it opened, its station is a setting rather than a constant, and the station it opens
is not asked about twice by the scan that finds the same URL in the catalogue.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import config_audit, db, gifs, media_health, media_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheScan(unittest.TestCase):
    def test_it_walks_every_declared_source(self):
        """A guard that found no files would pass every test below forever."""
        for entry in media_health.SOURCE_PATHS:
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, entry)),
                f"media_health watches {entry}, which no longer exists",
            )

    def test_it_finds_the_media_that_is_really_written_down(self):
        found = media_health.scan_source(ROOT)
        self.assertTrue(found, "the scan found nothing in a tree with links in it")
        self.assertGreaterEqual(len(found), 8, "the real tree has more media than this")
        for target in found:
            self.assertEqual(target.kind, "file")
            self.assertTrue(target.url.startswith("http"))
            self.assertTrue(media_health._is_media_url(target.url), target.url)
            # The name says where to fix it, because the card prints it.
            self.assertIn(".", target.source, f"{target.name} does not name a file")

    def test_it_never_reports_a_deliberately_broken_url_from_a_test(self):
        for target in media_health.scan_source(ROOT):
            parts = target.source.split(os.sep)
            self.assertNotIn("tests", parts, target.name)
            self.assertNotIn("docs", parts, target.name)
            self.assertNotIn("deleted_features", parts, target.name)

    def test_the_dead_links_this_watch_was_built_from_are_gone(self):
        """The two marriage GIFs answered 404 and were still in the tree when
        this module was written — found by this very scan."""
        urls = {target.url for target in media_health.scan_source(ROOT)}
        self.assertNotIn("https://media.tenor.com/E8w9k1nJ7W0AAAAC/anime-wedding.gif", urls)
        self.assertNotIn("https://media.tenor.com/7Ie9b4Wc3zUAAAAC/ring-anime.gif", urls)

    def test_a_template_is_not_a_url_yet(self):
        self.assertFalse(media_health._is_media_url("https://cdn.discordapp.com/avatars/{uid}/{h}.png"))
        self.assertFalse(media_health._is_media_url("https://api.purrbot.site/v2/img/sfw/{reaction}/gif"))

    def test_a_bare_host_is_not_a_picture(self):
        """The first real run of this scan reported ``utils/discord_auth.py:305``
        as a dead link: it is ``CDN_HOST``, the prefix avatar URLs are built from,
        and it answers 403 because there is nothing at it to fetch."""
        self.assertFalse(media_health._is_media_url("https://cdn.discordapp.com/"))
        self.assertFalse(media_health._is_media_url("https://cataas.com"))
        self.assertFalse(media_health._is_media_url("https://media.tenor.com"))
        self.assertTrue(media_health._is_media_url("https://cataas.com/cat"))

    def test_api_endpoints_and_pages_are_not_media(self):
        self.assertFalse(media_health._is_media_url("https://api.thecatapi.com/v1/images/search"))
        self.assertFalse(media_health._is_media_url("https://meow-bot.pages.dev/dashboard"))
        self.assertTrue(media_health._is_media_url("https://i.imgur.com/2s8K6Hh.jpeg"))
        self.assertTrue(media_health._is_media_url("https://media.tenor.com/anything/at/all"))

    def test_it_reads_a_tree_written_for_the_test(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "cogs"))
            with open(os.path.join(folder, "cogs", "card.py"), "w", encoding="utf-8") as handle:
                handle.write('ART = "https://i.imgur.com/ok.gif"\n')
                handle.write('API = "https://api.example.com/v1/search"\n')
            os.makedirs(os.path.join(folder, "tests"))
            with open(os.path.join(folder, "tests", "t.py"), "w", encoding="utf-8") as handle:
                handle.write('BAD = "https://i.imgur.com/broken.gif"\n')
            found = media_health.scan_source(folder)
        self.assertEqual([target.url for target in found], ["https://i.imgur.com/ok.gif"])
        self.assertEqual(found[0].name, "cogs/card.py:1")


class WhatIsDeclared(unittest.TestCase):
    def test_every_action_and_pet_the_resolver_serves_is_checked(self):
        names = {target.name for target in media_health.declared()}
        for action in gifs.ROUTES:
            self.assertIn(f"action:{action}", names)
        for kind in gifs.PET_SOURCES:
            self.assertIn(f"pet:{kind}", names)

    def test_a_new_action_cannot_skip_the_check(self):
        mine = dict(gifs.ROUTES)
        mine["invented"] = (("purrbot", "hug"),)
        with mock.patch.object(gifs, "ROUTES", mine):
            self.assertIn("action:invented", {t.name for t in media_health.declared()})

    def test_targets_is_everything_that_is_written_and_everything_that_is_made(self):
        everything = media_health.targets(ROOT)
        declared_targets = media_health.declared()
        claimed = {target.url for target in declared_targets if target.url}
        scanned = [t for t in media_health.scan_source(ROOT) if t.url not in claimed]
        self.assertEqual(len(everything), len(declared_targets) + len(scanned))

    def test_the_stream_path_is_the_last_target_and_only_one(self):
        everything = media_health.targets(ROOT)
        streams = [t for t in everything if t.kind == "stream"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].name, "stream:radio")
        self.assertEqual(streams[0].url, media_source.probe_target())
        self.assertEqual(streams[0].source, "utils/media_source.py")
        # Declared first, scanned after: the *made* targets are the ones a reader
        # can act on, and the stream target is the last of them.
        self.assertIs(everything[len(media_health.declared()) - 1], streams[0])
        self.assertEqual(streams[0].kind, everything[len(media_health.declared()) - 1].kind)

    def test_the_station_it_opens_is_claimed_rather_than_probed_twice(self):
        """The probe station is *in the catalogue*, so the scan finds it too.

        A declared target wins the name for a URL it already names: two probes of one
        address is a wasted request and a count that overstates how much of the
        catalogue was checked — and the name it keeps is the one that says *what*
        broke rather than where the URL happens to be written.
        """
        urls = [target.url for target in media_health.targets(ROOT) if target.url]
        self.assertEqual(len(urls), len(set(urls)), "one URL, one probe")
        self.assertIn(media_source.probe_target(), urls)

    def test_the_probe_station_is_a_setting_and_has_a_default(self):
        """An operator whose network a station cannot reach can point the check at
        another one without a release — and with nothing set, it opens the default."""
        self.assertEqual(media_source.probe_target(), media_source.DEFAULT_PROBE_TARGET)
        with mock.patch.object(config_audit, "get", lambda key, *a: "https://example.test/stream"):
            self.assertEqual(media_source.probe_target(), "https://example.test/stream",)
            self.assertEqual(
                [t.url for t in media_health.declared() if t.kind == "stream"],
                ["https://example.test/stream"],
            )
        self.assertEqual(
            media_source.probe_target(),
            media_source.DEFAULT_PROBE_TARGET,
            "the default is what a deployment with no setting asks for",
        )


def _image(status=200, content_type="image/gif"):
    return {"status": status, "content_type": content_type}


async def _stream_ok():
    """The stream path as a working one answers, for the cases about links."""
    return True, ""


class _Content:
    async def read(self, _n=None):
        return b"GIF89a"


class _Response:
    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self.headers = {"Content-Type": spec.get("content_type", "image/gif")}
        self.content = _Content()
        self.released = False

    def release(self):
        self.released = True


class _Session:
    """A URL-to-answer table. A URL that is absent 404s."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        return _Response(self.routes.get(url, {"status": 404, "content_type": "text/html"}))


#: What the stubbed resolver hands back for every declared action/pet unless a
#: test names that target: a picture the fake session answers with real media. The
#: declared half is then healthy, so a test's own links are the only thing it
#: finds — which is what makes the assertions about *those* mean something.
RESOLVED_OK = "https://cdn.example.invalid/ok.gif"


class OnePass(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        media_health.reset()

    async def _check(self, urls, *, live=True, resolved=None, stream=None):
        """One pass over a tree of made-up links, with the resolver stubbed.

        ``live`` says whether the links written into that tree answer with media
        or are gone; ``resolved`` names the declared targets whose resolver answer
        should be something other than healthy, and a value of ``None`` there
        means it produced nothing at all.

        The stream path is stubbed for the same reason the resolver is: the real
        one opens a station over the network, and a test that needs an icecast
        server to be reachable is a test that fails for reasons that are not this
        code. ``stream`` is the reason to report instead, when a case wants one.
        """
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "cogs"))
            with open(os.path.join(folder, "cogs", "card.py"), "w", encoding="utf-8") as handle:
                for index, url in enumerate(urls):
                    handle.write(f'CARD{index} = "{url}"\n')

            async def fake_resolve(target):
                if resolved and target.name in resolved:
                    answer = resolved[target.name]
                    return answer, ("" if answer else media_health.LEFT_EMPTY)
                return RESOLVED_OK, ""

            async def fake_open_stream():
                return (False, stream) if stream else (True, "")

            routes = {url: _image() for url in urls} if live else {}
            routes[RESOLVED_OK] = _image()
            session = _Session(routes)
            with mock.patch.object(media_health, "_resolve", new=fake_resolve), \
                    mock.patch.object(media_health, "_open_stream", new=fake_open_stream):
                return await media_health.check(session=session, root=folder, now=1000)

    async def test_a_healthy_tree_is_clean(self):
        verdict = await self._check(["https://i.imgur.com/ok.gif"])
        self.assertTrue(verdict.healthy)
        self.assertEqual(verdict.dead, {})
        self.assertEqual(verdict.unchecked, {})
        self.assertEqual(verdict.checked, len(media_health.declared()) + 1)

    async def test_a_404_is_named_with_where_it_is_written(self):
        verdict = await self._check(["https://i.imgur.com/gone.gif"], live=False)
        self.assertEqual(list(verdict.names), ["cogs/card.py:1"])
        self.assertEqual(verdict.dead["cogs/card.py:1"], "HTTP 404")
        self.assertEqual(verdict.places["cogs/card.py:1"], "https://i.imgur.com/gone.gif")

    async def test_a_page_served_as_200_is_not_media(self):
        class _Wrong(_Session):
            async def get(self, url, **kwargs):
                self.calls.append(url)
                return _Response({"status": 200, "content_type": "text/html; charset=utf-8"})

        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "cogs"))
            with open(os.path.join(folder, "cogs", "card.py"), "w", encoding="utf-8") as handle:
                handle.write('CARD = "https://i.imgur.com/notreally.gif"\n')

            async def healthy_resolve(_target):
                return RESOLVED_OK, ""

            with mock.patch.object(media_health, "_resolve", new=healthy_resolve), \
                    mock.patch.object(media_health, "_open_stream", new=_stream_ok):
                verdict = await media_health.check(session=_Wrong({}), root=folder, now=1000)
        self.assertIn("not media", verdict.dead["cogs/card.py:1"])

    async def test_no_answer_is_unchecked_rather_than_dead(self):
        """"This link is broken" and "I could not reach anybody" are different
        claims, and only one of them sends somebody to change a URL."""
        class _Dead(_Session):
            async def get(self, url, **kwargs):
                raise TimeoutError("no route to host")

        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "cogs"))
            with open(os.path.join(folder, "cogs", "card.py"), "w", encoding="utf-8") as handle:
                handle.write('CARD = "https://i.imgur.com/unreachable.gif"\n')

            async def healthy_resolve(_target):
                return RESOLVED_OK, ""

            with mock.patch.object(media_health, "_resolve", new=healthy_resolve), \
                    mock.patch.object(media_health, "_open_stream", new=_stream_ok):
                verdict = await media_health.check(session=_Dead({}), root=folder, now=1000)
        self.assertEqual(verdict.dead, {})
        self.assertIn("no answer", verdict.unchecked["cogs/card.py:1"])
        self.assertTrue(verdict.healthy, "nothing dead was found")

    async def test_a_resolver_that_produces_nothing_is_a_finding(self):
        verdict = await self._check([], resolved={"action:poke": None})
        self.assertIn("action:poke", verdict.dead)
        self.assertIn("produced nothing", verdict.dead["action:poke"])
        self.assertIsNone(verdict.places.get("action:poke"))
        self.assertEqual(verdict.kinds["action:poke"], "action")

    async def test_a_resolved_url_that_lives_is_not_a_finding(self):
        verdict = await self._check([], resolved={"pet:cat": "https://cataas.com/cat/gif"})
        self.assertNotIn("pet:cat", verdict.dead)
        self.assertNotIn("pet:cat", verdict.kinds)

    async def test_a_stream_path_that_will_not_open_is_a_finding_about_a_station(self):
        """The one target that is not a link, on a day every link is healthy.

        Nothing else in this pass would notice: the catalogue's own URLs are probed
        as URLs and answer, and no picture or sound in the tree has moved. What is
        broken is the *path* — this host cannot open a station it always could — and
        the finding names it as such rather than as a dead picture.
        """
        verdict = await self._check(
            ["https://i.imgur.com/ok.gif"],
            stream="the station answered HTTP 403",
        )
        self.assertEqual(list(verdict.names), ["stream:radio"])
        self.assertEqual(verdict.dead["stream:radio"], "the station answered HTTP 403")
        self.assertEqual(verdict.kinds["stream:radio"], "stream")
        # It *does* name a URL, unlike the extractor it replaced: the station is a
        # link, so there is something for a reader to open and see for themselves.
        self.assertEqual(verdict.places["stream:radio"], media_source.probe_target())
        self.assertFalse(verdict.healthy)
        self.assertIn("stream:radio", media_health.describe(verdict))
        self.assertIn("stream:radio", media_health.card_body(verdict))

    async def test_a_working_stream_path_is_not_a_finding(self):
        verdict = await self._check([])
        self.assertNotIn("stream:radio", verdict.dead)
        self.assertNotIn("stream:radio", verdict.kinds)

    async def test_the_stream_state_a_console_reads_comes_from_that_verdict(self):
        """No second probe, and an honest state when there is no verdict yet.

        The console polls and ``/health`` is unauthenticated, so neither may open a
        station: both read the last pass. *Not asked since this process started* is a
        state a reader can act on (``/music doctor`` asks now), which is why it is its
        own status rather than being folded into "broken".
        """
        media_health.reset()
        self.assertEqual(media_health.stream_state()["status"], "unknown")
        self.assertIsNone(media_health.stream_state()["checked_seconds_ago"])

        healthy = await self._check([])
        media_health.record(healthy)
        state = media_health.stream_state()
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["detail"], "")
        self.assertIsNotNone(state["checked_seconds_ago"])

    async def test_a_station_refusing_the_request_is_a_failure_not_an_unknown(self):
        """`unreachable` is about *this host's* network and nothing else.

        A station that answers 403 has answered, and calling that "could not ask"
        would hide the one failure an operator can go and see for themselves.
        """
        verdict = await self._check([], stream="the station answered HTTP 403")
        media_health.record(verdict)
        state = media_health.stream_state()
        self.assertEqual(state["status"], "failed")
        self.assertIn("403", state["detail"])

    async def test_a_network_that_cannot_reach_any_station_is_not_a_finding(self):
        """The contract between ``_open_stream`` and the pass: a station refusing
        the request is a *finding*, and a host with no route out is *unchecked*.
        Only one of those is worth waking somebody for, and the pass tells them
        apart by the reason's own shape — a host with no network is not a booking
        about a stream."""
        probe = media_source.Probe(
            healthy=False,
            reason="this host could not open the station (URLError)",
            target="https://example.test/stream",
            unreachable=True,
            at=1000.0,
        )
        with mock.patch.object(media_source, "self_check", lambda *a, **k: probe):
            healthy, reason = await media_health._open_stream()
        self.assertFalse(healthy)
        self.assertTrue(reason.startswith("no answer"), reason)

        refused = media_source.Probe(
            healthy=False,
            reason="the station answered HTTP 404",
            target="https://example.test/stream",
            unreachable=False,
            at=1000.0,
        )
        with mock.patch.object(media_source, "self_check", lambda *a, **k: refused):
            healthy, reason = await media_health._open_stream()
        self.assertFalse(healthy)
        self.assertFalse(reason.startswith("no answer"), reason)
        self.assertIn("404", reason, "the answer travels with the finding")

    async def test_a_probe_that_could_say_nothing_still_names_the_mechanism(self):
        """A probe with an empty reason must not report an empty finding."""
        blank = media_source.Probe(healthy=False, reason="", target="", at=1000.0)
        with mock.patch.object(media_source, "self_check", lambda *a, **k: blank):
            healthy, reason = await media_health._open_stream()
        self.assertFalse(healthy)
        self.assertEqual(reason, media_health.STREAM_FAILED)

    async def test_the_counts_add_up(self):
        verdict = await self._check([], resolved={"pet:cat": None})
        counts = verdict.counts
        self.assertEqual(counts["checked"], verdict.checked)
        self.assertEqual(
            counts["ok"] + counts["dead"] + counts["unchecked"], counts["checked"]
        )

    async def test_it_never_raises_on_a_broken_session(self):
        class _Exploding:
            async def get(self, *args, **kwargs):
                raise RuntimeError("boom")

        async def healthy_resolve(_target):
            return RESOLVED_OK, ""

        with mock.patch.object(media_health, "_resolve", new=healthy_resolve), \
                mock.patch.object(media_health, "_open_stream", new=_stream_ok):
            verdict = await media_health.check(session=_Exploding(), root=ROOT, now=1000)
        self.assertGreater(verdict.checked, 0)
        self.assertTrue(verdict.unchecked)


class TheHealthEndpoint(unittest.TestCase):
    """``/health`` is where the owner sees this without reading a chat.

    Driven through Flask, because a dict nothing renders is not a fix — the same
    reason the site guard's block is tested this way.
    """

    def setUp(self):
        media_health.reset()

    def test_the_live_endpoint_serves_it(self):
        url = "https://media.tenor.com/secret-looking/wedding.gif"
        media_health.record(
            media_health.Verdict(
                at=int(time.time()),
                checked=23,
                dead={"cogs/social.py:293": "HTTP 404"},
                places={"cogs/social.py:293": url},
                kinds={"cogs/social.py:293": "file"},
            )
        )
        from keep_alive import app

        with app.test_client() as client:
            body = client.get("/health").get_json()
        self.assertIn("media", body)
        self.assertEqual(body["media"]["status"], "attention")
        self.assertEqual(body["media"]["dead"], 1)
        # Counts and no names: this endpoint is unauthenticated, and the card that
        # names the dead link goes to the operator's own channel.
        blob = json.dumps(body)
        self.assertNotIn(url, blob)
        self.assertNotIn("social.py", blob)

    def test_a_deployment_that_has_not_checked_says_so(self):
        from keep_alive import app

        with app.test_client() as client:
            body = client.get("/health").get_json()
        self.assertEqual(body["media"]["status"], "unknown")


class TheWeeklyLoop(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """What ``keep_alive`` does with a pass, which is where the promise lives.

    The check itself is tested above; this is the clock and the delivery — the two
    halves that decide whether a dead link is ever actually said out loud, or
    whether forty URLs get probed weekly and nothing happens.
    """

    async def asyncSetUp(self):
        self.use_temp_database("media-loop.db")
        await db.setup()
        media_health.reset()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_nothing_runs_while_the_last_pass_is_fresh(self):
        import keep_alive

        await media_health.mark_ran(now=int(time.time()))
        with mock.patch.object(media_health, "check", new=mock.AsyncMock()) as check:
            await keep_alive._run_media_check()
        check.assert_not_awaited()

    async def test_a_due_pass_is_recorded_marked_and_said(self):
        import keep_alive
        from utils import operator_log

        verdict = media_health.Verdict(at=999, checked=23, dead={"cogs/social.py:293": "HTTP 404"})
        said = []

        async def fake_check(**_kwargs):
            return verdict

        async def fake_alert(_bot, _verdict, message):
            said.append(message)
            return True

        with mock.patch.object(media_health, "check", new=fake_check), \
                mock.patch.object(operator_log, "media_alert", new=fake_alert), \
                mock.patch.object(keep_alive, "BOT_INSTANCE", object()):
            await keep_alive._run_media_check()

        self.assertIs(media_health.last(), verdict, "the pass is what /health reports")
        self.assertFalse(await media_health.due(), "a pass that ran is not due again")
        self.assertEqual(len(said), 1)
        self.assertIn("cogs/social.py:293", said[0])

    async def test_it_says_the_same_thing_only_once(self):
        import keep_alive
        from utils import operator_log

        verdict = media_health.Verdict(at=999, checked=23, dead={"cogs/social.py:293": "HTTP 404"})
        said = []

        async def fake_check(**_kwargs):
            return verdict

        async def fake_alert(_bot, _verdict, message):
            said.append(message)
            return True

        with mock.patch.object(media_health, "check", new=fake_check), \
                mock.patch.object(operator_log, "media_alert", new=fake_alert), \
                mock.patch.object(keep_alive, "BOT_INSTANCE", object()):
            await keep_alive._run_media_check()
            await media_health.mark_ran(now=0)  # pretend a week went by
            await keep_alive._run_media_check()
        self.assertEqual(len(said), 1, "the same four dead links are not four cards")

    async def test_a_clean_pass_says_nothing_at_all(self):
        import keep_alive
        from utils import operator_log

        async def fake_check(**_kwargs):
            return media_health.Verdict(at=999, checked=23)

        async def fake_alert(*_args, **_kwargs):
            raise AssertionError("a clean week must be silence")

        with mock.patch.object(media_health, "check", new=fake_check), \
                mock.patch.object(operator_log, "media_alert", new=fake_alert):
            await keep_alive._run_media_check()

    async def test_it_starts_on_the_bots_own_loop(self):
        import keep_alive

        class _Loop:
            def __init__(self):
                self.started = None

            def create_task(self, coro):
                self.started = coro
                coro.close()
                return "task"

        class _Bot:
            loop = _Loop()

        self.assertEqual(keep_alive.start_media_health(_Bot()), "task")
        self.assertIsNotNone(_Bot.loop.started)
        # ...and a boot without a loop warns rather than raising into on_ready.
        self.assertIsNone(keep_alive.start_media_health(object()))
        self.assertIsNone(keep_alive.start_media_health(None))


class WhatItSays(unittest.TestCase):
    def setUp(self):
        media_health.reset()

    def _verdict(self, **overrides):
        base = dict(at=1000, checked=10, dead={"cogs/card.py:3": "HTTP 404"}, places={})
        base.update(overrides)
        return media_health.Verdict(**base)

    def test_a_clean_pass_is_silence(self):
        verdict = media_health.Verdict(at=1000, checked=10)
        self.assertIsNone(media_health.note(verdict))

    def test_a_dead_link_is_said_once_per_pass(self):
        verdict = self._verdict()
        self.assertIsNotNone(media_health.note(verdict))
        self.assertIsNone(media_health.note(verdict), "the same pass was reported twice")

    def test_the_next_pass_says_again(self):
        media_health.note(self._verdict())
        self.assertIsNotNone(media_health.note(self._verdict(at=2000)))

    def test_a_pass_that_found_nothing_dead_clears_the_memory(self):
        media_health.note(self._verdict())
        media_health.note(media_health.Verdict(at=2000, checked=10))
        self.assertIsNotNone(media_health.note(self._verdict(at=3000)))

    def test_describe_names_the_target_and_the_reason(self):
        text = media_health.describe(self._verdict())
        self.assertIn("cogs/card.py:3", text)
        self.assertIn("HTTP 404", text)

    def test_the_card_body_carries_the_url_to_change(self):
        body = media_health.card_body(
            self._verdict(places={"cogs/card.py:3": "https://i.imgur.com/gone.gif"})
        )
        self.assertIn("https://i.imgur.com/gone.gif", body)

    def test_the_card_body_is_bounded(self):
        many = {f"cogs/card.py:{n}": "HTTP 404" for n in range(400)}
        body = media_health.card_body(self._verdict(dead=many))
        self.assertLessEqual(len(body), media_health.MAX_CARD_CHARS)


class TheHealthBlock(unittest.TestCase):
    def setUp(self):
        media_health.reset()

    def test_unknown_before_the_first_pass(self):
        self.assertEqual(media_health.health_json()["status"], "unknown")

    def test_it_counts_and_never_names(self):
        url = "https://i.imgur.com/secret-looking.gif"
        media_health.record(
            media_health.Verdict(
                at=1000, checked=4, dead={"cogs/card.py:1": "HTTP 404"}, places={"cogs/card.py:1": url}
            )
        )
        payload = media_health.health_json()
        self.assertEqual(payload["status"], "attention")
        self.assertEqual(payload["targets"], 4)
        self.assertEqual(payload["dead"], 1)
        blob = str(payload)
        self.assertNotIn(url, blob)
        self.assertNotIn("card.py", blob)

    def test_a_clean_pass_reads_ok(self):
        media_health.record(media_health.Verdict(at=1000, checked=4))
        self.assertEqual(media_health.health_json()["status"], "ok")


class TheWeeklyClock(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("media_health.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()
        os.environ.pop("MEDIA_CHECK_INTERVAL_HOURS", None)

    async def test_a_deployment_that_never_ran_is_due(self):
        self.assertTrue(await media_health.due(now=1000))

    async def test_a_pass_that_just_ran_is_not_due_again(self):
        await media_health.mark_ran(now=1000)
        self.assertFalse(await media_health.due(now=1000 + 60))
        self.assertFalse(await media_health.due(now=1000 + 86400))

    async def test_a_week_later_it_is_due_again(self):
        await media_health.mark_ran(now=1000)
        self.assertTrue(await media_health.due(now=1000 + 7 * 24 * 3600))

    async def test_the_interval_is_a_setting_and_has_a_floor(self):
        self.assertEqual(media_health.interval_seconds(), 168 * 3600)
        os.environ["MEDIA_CHECK_INTERVAL_HOURS"] = "2"
        self.assertEqual(media_health.interval_seconds(), 7200)
        os.environ["MEDIA_CHECK_INTERVAL_HOURS"] = "0"
        self.assertEqual(media_health.interval_seconds(), 3600, "zero would mean a pass per wake")
        os.environ["MEDIA_CHECK_INTERVAL_HOURS"] = "nonsense"
        self.assertEqual(media_health.interval_seconds(), 168 * 3600)

    async def test_an_unreadable_row_does_not_stop_the_check(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (media_health.GLOBAL_ROW, media_health.LAST_RUN_KEY, "not a number"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(await media_health.due(now=1000))

    async def test_the_setting_is_declared(self):
        """A read the registry does not know about is a setting that vanishes
        from the report that exists to list them all."""
        self.assertIn(
            "MEDIA_CHECK_INTERVAL_HOURS", {setting.name for setting in config_audit.SETTINGS}
        )


class ItNeverBlocksTheBot(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_pass_over_the_real_tree_finishes(self):
        """One pass with no network at all: every target must land in ``unchecked``
        rather than raising, because this runs inside the bot's own loop.

        The stream check is stubbed *as offline* rather than left real: two of the
        findings here are about a network that is not there, and a pass that opened
        a real station would be testing the opposite of what it claims.
        """
        class _Offline:
            async def get(self, *args, **kwargs):
                raise OSError("network is unreachable")

        async def healthy_resolve(_target):
            return RESOLVED_OK, ""

        async def offline_stream():
            return False, "no answer (this host could not open the station)"

        media_health.reset()
        with mock.patch.object(media_health, "_resolve", new=healthy_resolve), \
                mock.patch.object(media_health, "_open_stream", new=offline_stream):
            verdict = await media_health.check(session=_Offline(), root=ROOT, now=1000)
        self.assertGreater(verdict.checked, 10)
        self.assertEqual(verdict.dead, {})
        self.assertTrue(verdict.healthy)


if __name__ == "__main__":
    unittest.main()
