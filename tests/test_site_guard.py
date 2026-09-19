"""The site must not be serving this repository, and the check must know that.

Everything here exists because of one incident and one near-miss.

The incident: on 2026-09-19 the Cloudflare Pages project published the
**checkout** — 185 files, `/bot.py` and `/render.yaml` and `/.env.example`
readable by anyone at the site's own address — because the build command and the
build output directory were both empty, and it reported **success** while doing
it. Nothing in the product looked wrong.

The near-miss: the first live run of :mod:`utils.site_guard` reported that
exposure as *still open*, and it was not. The deployment was clean; seven paths
answered `200` with real source, `cf-cache-status: HIT`, `age: 2073` — CDN copies
left from the bad deploy, served at the bare URL half an hour after the origin
had begun refusing it. So the very first thing the guard ever said was wrong, and
it would have sent its reader to fix build settings that were already correct.

That is the failure this file is mostly about. A smoke check has two ways to be
worthless and they are opposite: a **false clean** (it says nothing when the tree
is public, which is the whole point of having it) and a **false alarm** (it says
the leak is back when it is not, which is how a guard gets muted before the day
it matters). Both are tested here, and the cache-busting is tested as the reason
the second one does not happen.

The tests therefore cover:

1. **what a probe is** — a `GET` to a unique, cache-busted URL, never a plain
   one, and the bare URL asked about only after the deployment has refused it;
2. **what an answer means** — the five verdicts and their precedence, including
   the host that answers everything and the host that answers nothing;
3. **what gets said** — an exposure every cycle, a standing condition once a day,
   and nothing at all when the tree is private;
4. **the one claim that can rot** — every probed path must exist in this
   checkout, or the guard is watching paths nobody has.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from utils import site_guard

ROOT = Path(__file__).resolve().parent.parent


def _fetch_from(table, *, control=404):
    """A fetcher that answers from ``table``, keyed by ``(path, bust)``.

    ``table`` holds the statuses for named paths; anything not named answers
    ``404``, which is the good answer. The control path is answered separately
    because it is the one probe whose *content* is not a repository file.
    """

    def fetch(path, *, bust=True):
        if path.startswith(site_guard.CONTROL_PREFIX):
            return control
        return table.get((path, bust), 404)

    return fetch


class ProbeShape(unittest.TestCase):
    """A probe has to ask the deployment, not the cache in front of it."""

    def setUp(self):
        site_guard.reset()

    def _request(self, path, *, bust):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            site_guard._fetch("https://example.test", path, bust=bust)
            return urlopen.call_args[0][0]

    def test_a_probe_is_never_head(self):
        """``HEAD`` would be cheaper and could read as *not exposed*.

        A host that answers ``405`` to ``HEAD`` would look clean while serving
        the file to a ``GET`` — a false clean, which is the one answer this must
        never give.
        """
        self.assertEqual(self._request("bot.py", bust=True).get_method(), "GET")
        self.assertEqual(self._request("bot.py", bust=False).get_method(), "GET")

    def test_an_authoritative_probe_carries_a_unique_cache_buster(self):
        """The defect the first live run found: a cached copy read as a leak.

        Two mechanisms, because they defeat different layers — a unique query
        string is a different URL to a CDN, and ``no-cache`` makes anything that
        still recognises the path revalidate instead of serving a stored copy.
        """
        first = self._request("bot.py", bust=True)
        second = self._request("bot.py", bust=True)
        self.assertIn("?site-guard=", first.full_url)
        self.assertIn("?site-guard=", second.full_url)
        self.assertNotEqual(
            first.full_url, second.full_url,
            "two cycles asked the same URL, so the second read a cached answer",
        )
        self.assertEqual(first.get_header("Cache-control"), "no-cache")

    def test_the_bare_url_is_asked_without_a_buster_and_without_no_cache(self):
        """It is asked *because* it may be a cache, so it must not defeat one."""
        request = self._request("bot.py", bust=False)
        self.assertNotIn("site-guard", request.full_url)
        self.assertIsNone(request.get_header("Cache-control"))

    def test_the_bare_url_is_only_asked_after_the_deployment_refuses(self):
        """A path the deployment *serves* is never asked about twice.

        The bare URL answers one question — is a copy left in a cache — and a
        path the deployment is already serving has answered the only question
        that matters, the worse way. Asking anyway would double the requests a
        real leak costs for an answer that cannot change it.
        """
        asked: list[tuple[str, bool]] = []

        def fetch(path, *, bust=True):
            asked.append((path, bust))
            if path == site_guard.CONTROL_PREFIX + "-0":
                return 404
            return 200 if path == "bot.py" and bust else 404

        site_guard.check("https://site.test", fetch=fetch)
        self.assertIn(("bot.py", True), asked)
        self.assertNotIn(("bot.py", False), asked,
                         "a served path was asked about a second time")
        self.assertIn(("render.yaml", False), asked,
                      "a refused path was not asked about its cached copy")

    def test_every_path_is_probed_authoritatively_exactly_once(self):
        asked: list[tuple[str, bool]] = []

        def fetch(path, *, bust=True):
            asked.append((path, bust))
            return 404

        site_guard.check("https://site.test", fetch=fetch)
        probed = [path for path, bust in asked
                  if bust and not path.startswith(site_guard.CONTROL_PREFIX)]
        self.assertEqual(sorted(probed), sorted(site_guard.PROBES))
        self.assertEqual(len(asked), 2 * len(site_guard.PROBES) + 1,
                         "the cycle is two questions per path plus one control")


class ProbesAgainstTheRepository(unittest.TestCase):
    """Every probed path must be a file this checkout really has."""

    def test_every_probed_path_exists(self):
        missing = [p for p in site_guard.PROBES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], f"probes name files that are not here: {missing}")

    def test_no_path_is_probed_twice(self):
        self.assertEqual(len(set(site_guard.PROBES)), len(site_guard.PROBES))

    def test_the_probes_cover_the_shapes_a_static_host_would_expose(self):
        """A root script, a package module, a config, a dotfile and a test.

        Named here so that a future edit has to say out loud that it dropped one,
        rather than quietly narrowing what counts as *the repository*.
        """
        joined = "\n".join(site_guard.PROBES)
        for shape in ("bot.py", "utils/", "cogs/", "render.yaml", ".env.example", "tests/"):
            self.assertIn(shape, joined)


class Verdicts(unittest.TestCase):
    """What each set of answers means — and the precedence between them."""

    def setUp(self):
        site_guard.reset()

    def test_a_site_that_refuses_everything_is_clean(self):
        verdict = site_guard.check("https://site.test", fetch=_fetch_from({}))
        self.assertEqual(verdict.status, site_guard.Verdict.CLEAN)
        self.assertEqual(verdict.exposed, ())
        self.assertEqual(verdict.cached_paths, ())
        self.assertEqual(verdict.counts["exposed"], 0)

    def test_a_path_the_deployment_serves_is_a_leak(self):
        verdict = site_guard.check(
            "https://site.test",
            fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
        )
        self.assertEqual(verdict.status, site_guard.Verdict.EXPOSED)
        self.assertEqual(verdict.exposed, ("bot.py",))

    def test_a_redirect_counts_as_still_talking_about_the_path(self):
        """``< 400``, not ``== 200``: a path that resolved somewhere resolved."""
        verdict = site_guard.check(
            "https://site.test", fetch=_fetch_from({("render.yaml", True): 302})
        )
        self.assertEqual(verdict.status, site_guard.Verdict.EXPOSED)

    def test_a_copy_left_in_the_cache_is_not_reported_as_a_leak(self):
        """The live near-miss, as a test: origin refuses, the CDN still serves.

        The status has to be its own thing, because the remedies differ. *Fix the
        build settings* is wrong here — they are correct — and acting on it while
        the copy keeps being handed out is worse than the original silence.
        """
        verdict = site_guard.check(
            "https://site.test",
            fetch=_fetch_from({("bot.py", False): 200, ("README.md", False): 200}),
        )
        self.assertEqual(verdict.status, site_guard.Verdict.CACHED)
        self.assertEqual(verdict.exposed, (), "a cache hit was called a live leak")
        self.assertEqual(verdict.cached_paths, ("README.md", "bot.py"))

    def test_a_cache_copy_outranks_an_unanswered_probe(self):
        """A found thing is reported before a missing answer.

        One path the deployment refuses and a cache still serves, and one that
        did not answer at all: the cached copy is actionable, so it is the
        verdict, and the unanswered probe is still counted rather than dropped.
        """
        def fetch(path, *, bust=True):
            if path.startswith(site_guard.CONTROL_PREFIX):
                return 404
            if path == "bot.py":
                return 404 if bust else 200
            if path == "README.md":
                return None
            return 404

        verdict = site_guard.check("https://site.test", fetch=fetch)
        self.assertEqual(verdict.status, site_guard.Verdict.CACHED)
        self.assertEqual(verdict.cached_paths, ("bot.py",))
        self.assertEqual(verdict.unanswered, ("README.md",))

    def test_silence_from_the_origin_is_not_attributed_to_a_cache(self):
        """Unanswered plus a ``200`` from the bare URL is still not `cached`.

        The bare URL cannot say *who* answered it, so calling it a cache would
        assert the deployment is fine on the strength of a probe that never
        reached the deployment.
        """
        def fetch(path, *, bust=True):
            if path.startswith(site_guard.CONTROL_PREFIX):
                return 404
            if path != "bot.py":
                return 404
            return None if bust else 200

        verdict = site_guard.check("https://site.test", fetch=fetch)
        self.assertEqual(verdict.cached_paths, ())
        self.assertEqual(verdict.status, site_guard.Verdict.UNREACHABLE)

    def test_a_host_that_answers_everything_is_not_a_leak(self):
        """Some hosts serve a page for any path. Then a ``200`` proves nothing.

        Reported as its own state rather than as a breach: calling it a leak
        would be a false alarm on every cycle, and calling it clean would be a
        false clean for as long as the fallback hides what is really there.
        """
        verdict = site_guard.check(
            "https://site.test",
            fetch=_fetch_from({("bot.py", True): 200}, control=200),
        )
        self.assertEqual(verdict.status, site_guard.Verdict.FALLBACK)

    def test_a_control_that_does_not_answer_does_not_hide_a_leak(self):
        verdict = site_guard.check(
            "https://site.test",
            fetch=_fetch_from({("bot.py", True): 200}, control=None),
        )
        self.assertEqual(verdict.status, site_guard.Verdict.EXPOSED)

    def test_no_answer_at_all_is_unreachable_never_clean(self):
        verdict = site_guard.check("https://site.test", fetch=lambda path, **_: None)
        self.assertEqual(verdict.status, site_guard.Verdict.UNREACHABLE)

    def test_one_unanswered_probe_makes_the_whole_check_unreachable(self):
        """A partly-answered check is a partly-answered check.

        The path that did not answer is a path that might have been public, so
        the verdict cannot be ``clean`` — it has to be the weaker claim.
        """
        def fetch(path, *, bust=True):
            return None if path == "README.md" else 404

        verdict = site_guard.check("https://site.test", fetch=fetch)
        self.assertEqual(verdict.status, site_guard.Verdict.UNREACHABLE)
        self.assertEqual(verdict.unanswered, ("README.md",))

    def test_an_error_status_is_not_an_answer_of_no(self):
        """``404`` is the good news, and it arrives as an exception in urllib."""
        with mock.patch("urllib.request.urlopen") as urlopen:
            import urllib.error
            urlopen.side_effect = urllib.error.HTTPError(
                "https://site.test/bot.py", 404, "Not Found", {}, None
            )
            self.assertEqual(site_guard._fetch("https://site.test", "bot.py"), 404)

    def test_a_transport_failure_is_not_404(self):
        """``None`` means the question was not asked — not that the answer was no."""
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = OSError("name resolution failed")
            self.assertIsNone(site_guard._fetch("https://site.test", "bot.py"))


class Reporting(unittest.TestCase):
    """What is worth a message, and how often."""

    def setUp(self):
        site_guard.reset()

    def _verdict(self, table, *, control=404, now=1_000_000):
        return site_guard.check("https://site.test", fetch=_fetch_from(table, control=control),
                                now=now)

    def test_a_private_tree_says_nothing(self):
        self.assertIsNone(site_guard.note(self._verdict({})))

    def test_an_exposure_is_said_every_cycle(self):
        """No throttle. A leak that is still open is the loudest thing here."""
        for moment in (1_000_000, 1_000_060, 1_000_120):
            verdict = site_guard.check(
                "https://site.test",
                fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
                now=moment,
            )
            self.assertIsNotNone(site_guard.note(verdict, now=moment))

    def test_a_standing_finding_is_said_once_a_day(self):
        table = {("bot.py", False): 200}
        first = self._verdict(table, now=1_000_000)
        self.assertIsNotNone(site_guard.note(first, now=1_000_000))
        second = self._verdict(table, now=1_000_060)
        self.assertIsNone(site_guard.note(second, now=1_000_060))
        third = self._verdict(table, now=1_000_000 + site_guard.QUIET_REPEAT_SECONDS)
        self.assertIsNotNone(site_guard.note(third, now=1_000_000 + site_guard.QUIET_REPEAT_SECONDS))

    def test_each_quiet_finding_is_throttled_on_its_own(self):
        """A blind site must not use up the one message a leak is owed."""
        blind = site_guard.check("https://site.test", fetch=lambda path, **_: None, now=1_000_000)
        self.assertIsNotNone(site_guard.note(blind, now=1_000_000))
        cached = self._verdict({("bot.py", False): 200}, now=1_000_001)
        self.assertIsNotNone(
            site_guard.note(cached, now=1_000_001),
            "one finding's throttle silenced a different finding",
        )

    def test_the_description_names_the_paths_for_a_leak_and_the_remedy_for_a_cache(self):
        leak = site_guard.describe(self._verdict({("bot.py", True): 200, ("bot.py", False): 200}))
        self.assertIn("bot.py", leak)
        stale = site_guard.describe(self._verdict({("bot.py", False): 200}))
        self.assertIn("cache", stale.lower())
        self.assertNotIn("EXPOSED", stale)


class Health(unittest.TestCase):
    """``/health`` carries the state, and carries no path names."""

    def setUp(self):
        site_guard.reset()

    def test_before_any_check_it_says_unknown_rather_than_clean(self):
        self.assertEqual(site_guard.health_json()["status"], "unknown")

    def test_after_a_check_it_reports_counts_and_no_paths(self):
        site_guard.record(
            site_guard.check(
                "https://site.test",
                fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
                now=1_000_000,
            )
        )
        payload = site_guard.health_json()
        self.assertEqual(payload["status"], site_guard.Verdict.EXPOSED)
        self.assertEqual(payload["exposed"], 1)
        self.assertNotIn("bot.py", json.dumps(payload))
        self.assertIn("checked_seconds_ago", payload)

    def test_the_live_endpoint_serves_it(self):
        """Driven through Flask, because a dict that is never rendered is not a fix."""
        site_guard.record(site_guard.check("https://site.test", fetch=_fetch_from({})))
        with keep_alive.app.test_client() as client:
            body = client.get("/health").get_json()
        self.assertIn("site_guard", body)
        self.assertEqual(body["site_guard"]["status"], site_guard.Verdict.CLEAN)
        self.assertEqual(body["site_guard"]["probes"], len(site_guard.PROBES))

    def test_the_report_does_not_change_the_bots_own_health(self):
        """Being wrongly configured is not the same claim as being unwell."""
        site_guard.record(
            site_guard.check(
                "https://site.test",
                fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
            )
        )
        with keep_alive.app.test_client() as client:
            body = client.get("/health").get_json()
        self.assertNotEqual(body["site_guard"]["status"], site_guard.Verdict.CLEAN)
        self.assertIn("status", body)


class TheAlert(unittest.TestCase):
    """The card is the whole detection, so it has to be right about which one."""

    def setUp(self):
        site_guard.reset()

    def _send(self, verdict):
        from utils import operator_log

        class _Channel:
            def __init__(self):
                self.embeds = []

        channel = _Channel()

        class _Guild:
            id = 1

        class _Bot:
            def get_guild(self, _id):
                return _Guild()

            def get_channel(self, _id):
                return channel

        async def _bound(_guild_id, _role):
            return 1

        patches = [
            mock.patch.object(operator_log.bot_channels, "bot_server_guild_id", return_value=1),
            mock.patch.object(operator_log.bot_channels, "bound", _bound),
            mock.patch.object(operator_log.style, "send_card",
                              side_effect=lambda target, embed=None, timeout=None:
                              _capture(channel, embed)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        import asyncio
        asyncio.run(operator_log.site_guard_alert(_Bot(), verdict))
        return channel

    def test_a_leak_is_danger_and_a_cached_copy_is_not(self):
        from utils import style

        leak = site_guard.check(
            "https://site.test",
            fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
        )
        stale = site_guard.check("https://site.test", fetch=_fetch_from({("bot.py", False): 200}))
        self.assertEqual(_colour(self._send(leak)), style.DANGER)
        self.assertEqual(_colour(self._send(stale)), style.WARN)

    def test_a_cached_copy_is_told_to_purge_and_a_leak_is_told_to_fix_the_build(self):
        """The remedies are different, so the cards must be too.

        Sending *fix the build settings* to somebody whose build settings are
        correct is how the reader learns the card is noise — and the cache keeps
        handing out the file while they check.
        """
        leak = self._send(
            site_guard.check(
                "https://site.test",
                fetch=_fetch_from({("bot.py", True): 200, ("bot.py", False): 200}),
            )
        )
        stale = self._send(site_guard.check("https://site.test",
                                            fetch=_fetch_from({("bot.py", False): 200})))
        self.assertIn("output directory", _text(leak))
        self.assertIn("urge", _text(stale))
        self.assertNotIn("output directory", _text(stale))

    def test_a_clean_verdict_never_reaches_the_card(self):
        sent = self._send(site_guard.check("https://site.test", fetch=_fetch_from({})))
        self.assertEqual(sent.embeds, [])


def _capture(channel, embed):
    channel.embeds.append(embed)
    return True


def _embed(channel):
    assert channel.embeds, "no card was sent"
    value = channel.embeds[-1]
    return value["embed"] if isinstance(value, dict) else value


def _colour(channel):
    return _embed(channel).colour.value


def _text(channel):
    embed = _embed(channel)
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts += [field.name or "", field.value or ""]
    return "\n".join(parts)


if __name__ == "__main__":
    unittest.main()
