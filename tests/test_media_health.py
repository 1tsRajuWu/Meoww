"""The media watch: what it looks at, and what it says when something is gone.

This exists because of a failure with no symptom anywhere: every roleplay card
pointed at a hand-typed ``media.tenor.com`` URL, all of them answered 404, and
what a member saw was an empty box Discord draws without complaining. So the
tests care about three things above the plumbing — that the *scan* really walks
the tree that may hold a link and skips the places that deliberately hold broken
ones, that a probe distinguishes "dead" from "could not ask", and that a clean
week is silence while a dead link is said exactly once.

"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import config_audit, db, gifs, media_health

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
        self.assertFalse(media_health._is_media_url("https://dacat.pages.dev/dashboard"))
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

def _image(status=200, content_type="image/gif"):
    return {"status": status, "content_type": content_type}


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

    async def _check(self, urls, *, live=True, resolved=None):
        """One pass over a tree of made-up links, with the resolver stubbed.

        ``live`` says whether the links written into that tree answer with media
        or are gone; ``resolved`` names the declared targets whose resolver answer
        should be something other than healthy, and a value of ``None`` there
        means it produced nothing at all.
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

            routes = {url: _image() for url in urls} if live else {}
            routes[RESOLVED_OK] = _image()
            session = _Session(routes)
            with mock.patch.object(media_health, "_resolve", new=fake_resolve):
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

            with mock.patch.object(media_health, "_resolve", new=healthy_resolve):
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

            with mock.patch.object(media_health, "_resolve", new=healthy_resolve):
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

        with mock.patch.object(media_health, "_resolve", new=healthy_resolve):
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

        Every finding here is about a network that is not there, which is the point:
        a pass that could reach anybody would be testing the opposite of what this
        one claims.
        """
        class _Offline:
            async def get(self, *args, **kwargs):
                raise OSError("network is unreachable")

        async def healthy_resolve(_target):
            return RESOLVED_OK, ""

        media_health.reset()
        with mock.patch.object(media_health, "_resolve", new=healthy_resolve):
            verdict = await media_health.check(session=_Offline(), root=ROOT, now=1000)
        self.assertGreater(verdict.checked, 10)
        self.assertEqual(verdict.dead, {})
        self.assertTrue(verdict.healthy)


if __name__ == "__main__":
    unittest.main()
