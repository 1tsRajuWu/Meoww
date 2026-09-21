"""The anti-AI posture, checked the way the framework asks for it to exist.

Three layers, and every test here is one of the claims the layer makes:

* **The directive** — ``robots.txt`` and ``ai.txt``, which are requests. What can
  be checked about a request is that it says the right thing per crawler, that
  the named crawlers come *before* the wildcard (a rule after ``User-agent: *``
  is read by nobody), and that the paths nobody should touch are in it.
* **The refusal** — a named AI crawler gets 403 on every path but the two files
  that tell it what the rules are. The tests that matter here are the *negative*
  ones: a browser, a monitor, a bare ``curl`` and a reader with no ``User-Agent``
  all have to pass through untouched, because the failure mode of a guard like
  this is refusing a person.
* **The trap** — a path no page links a person to, disallowed in the directives,
  absent from every sitemap, off-screen and unfocusable in the markup, and
  recorded when it is fetched. Its whole value is that its *absence* from
  legitimate traffic is what makes a fetch meaningful, so the tests assert that
  absence from both ends: the page that carries the link, and the file that
  forbids it.

The last class is the one that would have caught this change going wrong: every
host writes its directives from ``utils.ai_guard``, so a future build script that
types ``User-agent: * / Allow: /`` by hand fails here rather than publishing a
host where the policy is missing.
"""
from __future__ import annotations

import json
import os
import re
import unittest

import keep_alive
from utils import ai_guard, hub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def client():
    return keep_alive.app.test_client()


class TheDirectives(unittest.TestCase):
    def setUp(self):
        self.robots = ai_guard.robots_txt(sitemap="https://meow.example/sitemap.xml")
        self.lines = self.robots.splitlines()

    def test_every_named_crawler_is_disallowed(self):
        for token, _label in ai_guard.AI_CRAWLERS:
            with self.subTest(token=token):
                self.assertIn(f"User-agent: {token}", self.robots)
        # …and none of them is merely listed. Each block has to carry its own
        # Disallow, because a `User-agent` line with no rule under it grants
        # everything the wildcard used to permit.
        blocks = re.findall(
            r"User-agent: (\S+)\n((?:[^U\n].*\n)*)", self.robots
        )
        self.assertGreater(len(blocks), len(ai_guard.AI_CRAWLERS) - 1)
        for token, body in blocks:
            if token in dict(ai_guard.AI_CRAWLERS):
                self.assertIn("Disallow: /", body, f"{token} is listed and still allowed")

    def test_the_named_crawlers_come_before_the_wildcard(self):
        """Order is the whole mechanism: a rule after `*` is read by nobody."""
        wildcard = self.robots.index("User-agent: *")
        for token, _label in ai_guard.AI_CRAWLERS:
            self.assertLess(
                self.robots.index(f"User-agent: {token}"),
                wildcard,
                f"{token} is disallowed *after* the wildcard, which decides nothing",
            )

    def test_the_signed_in_surfaces_are_disallowed_for_everyone(self):
        tail = self.robots[self.robots.index("User-agent: *") :] if "User-agent: *" in self.robots else self.robots
        for path in ("/dashboard", "/servers", "/owner", "/login", "/api/"):
            self.assertIn(f"Disallow: {path}", tail, f"{path} is crawlable")

    def test_every_trap_is_disallowed_and_its_absence_from_a_sitemap_is_the_point(self):
        for path, _label in ai_guard.HONEYTOKENS:
            self.assertIn(f"Disallow: {path}", self.robots)
        # The sitemap is the other half of "nothing legitimate goes there": the
        # build script's page list and the hub's must not name one.
        self.assertNotIn(ai_guard.HONEYTOKENS[0][0], read("scripts/build_site.py"))
        self.assertNotIn(ai_guard.HONEYTOKENS[0][0], read("scripts/build_hub.py"))

    def test_a_private_host_disallows_everything(self):
        private = ai_guard.robots_txt(private=True)
        self.assertIn("User-agent: *\nDisallow: /\n", private)
        self.assertNotIn("Allow: /", private)
        for token, _label in ai_guard.AI_CRAWLERS:
            self.assertIn(f"User-agent: {token}", private)

    def test_the_sitemap_line_is_the_hosts_own_and_optional(self):
        self.assertIn("Sitemap: https://meow.example/sitemap.xml", self.robots)
        self.assertNotIn("Sitemap:", ai_guard.robots_txt())

    def test_ai_txt_says_no_training_in_the_form_crawlers_read(self):
        text = ai_guard.ai_txt()
        self.assertIn("User-agent: *", text)
        self.assertIn("Disallow: /", text)
        self.assertIn("tdm-reservation: 1", text)
        self.assertIn("content-usage: train-ai=n", text)
        self.assertIn("/ai", text, "the human-readable policy is not pointed at")

    def test_the_directives_name_the_policy_they_are_enforcing(self):
        self.assertIn(f"{ai_guard.policy_url()}/ai.txt", self.robots)
        self.assertIn("not permitted", self.robots)
        self.assertIn("terms of use", ai_guard.ai_txt())


class TheRefusal(unittest.TestCase):
    """Who is refused, and — the harder half — who is not."""

    def test_the_published_tokens_are_recognised(self):
        for token, _label in ai_guard.AI_CRAWLERS:
            with self.subTest(token=token):
                agent = f"Mozilla/5.0 (compatible; {token}/1.2; +https://example.test/bot)"
                self.assertEqual(ai_guard.crawler(agent), token)

    def test_a_person_is_never_refused(self):
        for agent in (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
            "UptimeRobot/2.0",
            "curl/8.4.0",
            "",
        ):
            with self.subTest(agent=agent[:40]):
                self.assertIsNone(ai_guard.crawler(agent), f"a real reader is refused: {agent!r}")

    def test_a_token_inside_a_longer_word_is_not_a_match(self):
        """`GPTBotSimulator` is somebody's test suite, not OpenAI."""
        self.assertIsNone(ai_guard.crawler("Mozilla/5.0 GPTBotSimulator/1.0"))
        self.assertIsNone(ai_guard.crawler("Mozilla/5.0 NotAClaudeBot/1.0"))

    def test_the_directive_files_are_the_only_exemption(self):
        for path in ai_guard.DIRECTIVE_PATHS:
            self.assertFalse(ai_guard.refuses(path), f"{path} is refused to the crawler it governs")
        for path in ("/", "/systems", "/dashboard", "/api/figures", "/privacy"):
            self.assertTrue(ai_guard.refuses(path))

    def test_the_app_refuses_a_declared_crawler_and_tells_it_what_it_is_ignoring(self):
        response = client().get(
            "/systems",
            headers={"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.2)"},
        )
        self.assertEqual(response.status_code, 403)
        body = response.get_data(as_text=True)
        self.assertIn("/terms", body)
        self.assertIn("/ai.txt", body)
        self.assertIn("403", body)

    def test_the_app_serves_that_crawler_the_rules_themselves(self):
        for path in ("/robots.txt", "/ai.txt", "/.well-known/ai.txt"):
            with self.subTest(path=path):
                response = client().get(path, headers={"User-Agent": "GPTBot/1.2"})
                self.assertEqual(response.status_code, 200, f"{path} is refused to the agent it governs")

    def test_the_app_does_not_refuse_a_browser(self):
        response = client().get(
            "/systems",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_the_reservation_travels_on_every_public_answer(self):
        response = client().get("/privacy")
        for name, value in ai_guard.TRAIN_HEADERS:
            self.assertEqual(response.headers.get(name), value, name)
        self.assertEqual(response.headers.get("tdm-policy"), f"{ai_guard.policy_url()}/ai")

    def test_the_signed_in_surfaces_are_noindex_as_well(self):
        response = client().get("/api/config")
        self.assertIn("noindex", response.headers.get("X-Robots-Tag", ""))


class TheTrap(unittest.TestCase):
    def setUp(self):
        # The counts are cached for a minute; a test that reads them has to start
        # from what this process has actually seen.
        ai_guard._summary = None

    def test_a_fetch_is_answered_with_a_decoy_and_recorded(self):
        before = ai_guard.totals()["traps"]
        response = client().get(ai_guard.HONEYTOKENS[0][0])
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.get_data(as_text=True))
        self.assertIn("not part of the site", payload["notice"])
        self.assertEqual(payload["dataset"], [])
        ai_guard._summary = None
        self.assertGreater(ai_guard.totals()["traps"], before, "the fetch was invisible")

    def test_a_query_string_does_not_walk_around_it(self):
        self.assertIsNotNone(ai_guard.trap(ai_guard.HONEYTOKENS[0][0] + "?cache=1"))
        response = client().get(ai_guard.HONEYTOKENS[0][0] + "?v=2")
        self.assertEqual(response.status_code, 200)

    def test_the_decoy_is_honest_and_carries_no_data_of_ours(self):
        """It says what it is: a poisoned payload invites an argument about who
        deceived whom, and the trap's value is that it cannot be argued with."""
        payload = json.loads(ai_guard.decoy(ai_guard.HONEYTOKENS[0][0]))
        self.assertEqual(payload["dataset"], [])
        self.assertIn("ignores robots.txt", payload["notice"])
        self.assertIn("/ai", payload["policy"]["ai"])
        self.assertEqual(payload["request"]["path"], ai_guard.HONEYTOKENS[0][0])

    def test_a_trap_is_recorded_once_with_a_count_rather_than_once_per_fetch(self):
        """Four hundred requests are one line with a count, or the evidence is
        unreadable exactly when somebody needs to read it."""
        path = ai_guard.HONEYTOKENS[1][0]
        first = ai_guard.record("honeytoken", agent="Collector/1.0", path=path, ip="203.0.113.9")
        second = ai_guard.record("honeytoken", agent="Collector/1.0", path=path, ip="203.0.113.9")
        self.assertEqual(first["hits"], 1)
        self.assertGreater(second["hits"], 1)

    def test_the_alert_fires_for_a_trap_and_not_for_a_single_refusal(self):
        ai_guard._alerted.clear()
        ai_guard._refused_since_alert.clear()
        trap = ai_guard.record("honeytoken", agent="Trap/1.0", path=ai_guard.HONEYTOKENS[0][0])
        self.assertTrue(ai_guard.should_alert(trap))
        refusal = ai_guard.record("crawler", agent="GPTBot", path="/systems")
        self.assertFalse(ai_guard.should_alert(refusal), "one refusal is a crawler finding out")

    def test_a_flood_of_refusals_from_one_agent_is_reported_once(self):
        ai_guard._alerted.clear()
        ai_guard._refused_since_alert.clear()
        alerts = [
            ai_guard.should_alert(ai_guard.record("crawler", agent="Flood/1.0", path="/systems"))
            for _ in range(ai_guard.FLOOD_THRESHOLD + 1)
        ]
        self.assertEqual(alerts.count(True), 1)

    def test_the_summary_names_nobody(self):
        """`/health` is unauthenticated by policy: counts and a date, no address."""
        block = client().get("/health").get_json()["anti_ai"]
        self.assertIn("refused", block)
        self.assertIn("traps", block)
        for key in ("ip", "agent", "path"):
            self.assertNotIn(key, block)


class TheMarkupAndTheHosts(unittest.TestCase):
    """One source, four hosts, and a link a person cannot follow."""

    def test_the_trap_link_matches_the_declared_path(self):
        body = read("templates/_trap.html")
        self.assertIn(f'href="{ai_guard.HONEYTOKENS[0][0]}"', body)
        self.assertIn('tabindex="-1"', body, "a keyboard user could land on the trap")
        self.assertIn("aria-hidden", body, "a screen reader would read the decoy out")

    def test_every_public_footer_carries_it(self):
        for template in ("templates/site_base.html", "templates/legal.html", "templates/hub/base.html"):
            with self.subTest(template=template):
                self.assertIn('include "_trap.html"', read(template))

    def test_the_pages_really_do_carry_it(self):
        body = client().get("/privacy").get_data(as_text=True)
        self.assertIn(ai_guard.HONEYTOKENS[0][0], body)

    def test_every_build_writes_its_directives_from_the_module(self):
        for script in ("scripts/build_site.py", "scripts/build_hub.py", "scripts/build_owner.py"):
            with self.subTest(script=script):
                source = read(script)
                self.assertIn("ai_guard.robots_txt(", source)
                self.assertIn("ai_guard.ai_txt()", source)
                self.assertNotIn('"User-agent: *\\nAllow: /\\n"', source)
                self.assertIn("ai_guard", source.split("import")[0] + source)

    def test_the_static_headers_files_carry_the_reservation(self):
        from scripts import build_owner, build_site

        public = build_site._headers_file()
        private = build_owner._headers_file()
        for name, value in ai_guard.TRAIN_HEADERS:
            with self.subTest(sheet="public", header=name):
                self.assertIn(f"{name}: {value}", public)
        self.assertIn("noindex", private, "the owner console is indexed as if it were public")
        self.assertIn("Content-Usage: train-ai=n", private)

    def test_the_hub_inherits_the_same_list_rather_than_writing_its_own(self):
        for name, value in ai_guard.TRAIN_HEADERS:
            self.assertIn((name, value), hub.SECURITY_HEADERS)

    def test_every_route_is_on_one_side_of_the_split(self):
        """A new route has to be classified, not defaulted.

        ``PUBLIC_PREFIXES`` and ``GATED_PREFIXES`` decide one thing that matters
        in this file and one that matters to the clickwrap: whether a page is
        served with ``noindex`` (every signed-in surface is) and whether the
        agreement stands in front of it. A route that is on neither list is
        handled as gated, which is the safe direction — and this test still fails
        on it, because "safe by accident" is how a console page ends up indexed.
        """
        known = (
            "/static/<path:filename>",
            "/hub/static/<path:name>",
        )
        def covered(path: str, prefixes) -> bool:
            return any(path == prefix or path.startswith(prefix) for prefix in prefixes)

        unclassified = []
        for rule in keep_alive.app.url_map.iter_rules():
            path = str(rule)
            if path in known:
                continue
            if covered(path, keep_alive.PUBLIC_EXACT):
                continue
            if not covered(path, keep_alive.PUBLIC_PREFIXES + keep_alive.GATED_PREFIXES):
                unclassified.append(path)
        self.assertEqual(
            unclassified,
            [],
            "these routes are neither public nor gated in keep_alive.PUBLIC_PREFIXES / "
            f"GATED_PREFIXES: {unclassified}",
        )

    def test_an_unlisted_path_is_gated_rather_than_shrugged_off(self):
        """Fail-closed, and *provably* so: the root's exact-match entry is what

        keeps ``PUBLIC_PREFIXES`` from becoming a list where ``"/"`` quietly
        covers everything. A path that appears in no list is the case this exists
        for, and it has to answer "gated" — the safe direction for a rule whose
        purpose is that a condition of use was shown before the page behind it
        worked.
        """
        self.assertTrue(keep_alive._agreement_stands_here("/api/secret-thing"))
        self.assertTrue(keep_alive._agreement_stands_here("/a-page-nobody-classified"))
        self.assertFalse(keep_alive._agreement_stands_here("/"))

    def test_the_gated_list_covers_the_console_and_the_dashboard(self):
        for path in ("/dashboard", "/servers", "/owner", "/api/console/fleet", "/api/config"):
            with self.subTest(path=path):
                self.assertTrue(keep_alive._agreement_stands_here(path), path)
        for path in ("/", "/privacy", "/terms", "/ai", "/health", "/ai.txt", "/robots.txt"):
            with self.subTest(path=path):
                self.assertFalse(keep_alive._agreement_stands_here(path), path)

    def test_the_dotted_hosts_agree(self):
        """`robots.txt` from the app and from a build differ only where they must."""
        app_host = client().get("/robots.txt").get_data(as_text=True)
        self.assertIn("# Automated collection and AI training are not permitted", app_host)
        built = ai_guard.robots_txt(sitemap="https://meow.example/sitemap.xml")
        without_sitemap = [line for line in built.splitlines() if not line.startswith("Sitemap:")]
        while without_sitemap and not without_sitemap[-1].strip():
            without_sitemap.pop()
        self.assertEqual(without_sitemap, app_host.rstrip("\n").splitlines())


if __name__ == "__main__":
    unittest.main()
