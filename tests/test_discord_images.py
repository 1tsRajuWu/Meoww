"""Discord's pictures reach the page, and only from our own origin.

Two defects met here, and they were the same defect seen from either end:

1. The dashboard sets ``img-src 'self' data:``. That policy is worth keeping — it
   is what stops a page whose template was tampered with from dialling out — but
   it **blocked every ``cdn.discordapp.com`` URL the templates emitted**, so
   server icons on ``/servers``, a signed-in person's picture and the bot's own
   avatar all rendered as the browser's broken-image glyph. Uploaded pictures
   worked only because ``/api/identity/image`` already served them from here.
   The first attempt at the avatar fallback found the avatar *correctly* and still
   showed a broken image, which is what this file pins.

2. The fix has to be origin-side rather than a wider policy, so the tests assert
   the policy is still narrow and that ``/api/image`` answers for exactly the
   pictures a session is already allowed to see.

``fetch_image`` fetches a URL, so it is also the one place in this project that
could be turned into an outbound-request gadget: the host check and the whitelist
are the tests that matter most here.
"""
import unittest
from unittest import mock

import keep_alive
from utils import discord_auth
from tests.test_composer import GUILD_ID, CHANNEL_ID, _Bot, _Channel, _Guild
from tests.test_dashboard import _DashboardFixture


class _Icon:
    """A guild icon asset: an address to fetch, and a version to bust a cache with."""

    def __init__(self, key="abc123", guild_id=GUILD_ID):
        self.key = key
        self.url = f"https://cdn.discordapp.com/icons/{guild_id}/{key}.png?size=1024"


class _Response:
    """Enough of an ``http.client.HTTPResponse`` for the fetcher to read."""

    def __init__(self, body=b"\x89PNG\r\n\x1a\n", content_type="image/png", status=200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _guild_with_icon():
    guild = _Guild(GUILD_ID, "Meow Server", [_Channel(CHANNEL_ID, "general")])
    guild.icon = _Icon()
    return guild


class TheOriginAddress(unittest.TestCase):
    """What the templates are handed: our own address, or nothing."""

    def setUp(self):
        self.guild = _guild_with_icon()
        self.bot = _Bot([self.guild])
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_a_server_picture_is_our_address_and_never_discords(self):
        """The regression itself: a CDN URL here is an image the CSP blocks.

        This is the test that would have caught the whole class of defect. It is
        written against the *address*, not against a render, because "the page
        loads" and "the picture arrives" are different claims and only the second
        one was ever broken.
        """
        address = keep_alive._guild_icon(self.guild.id)
        self.assertTrue(address.startswith("/api/image?"), address)
        self.assertNotIn("cdn.discordapp.com", address)
        self.assertIn(f"guild_id={GUILD_ID}", address)

    def test_the_icon_hash_travels_so_a_changed_picture_is_a_changed_url(self):
        address = keep_alive._guild_icon(self.guild.id)
        self.assertIn("v=abc123", address)
        self.guild.icon.url = "https://cdn.discordapp.com/icons/1/new.png"
        self.guild.icon.key = "new"
        self.assertNotEqual(address, keep_alive._guild_icon(self.guild.id))

    def test_a_server_with_no_picture_gets_an_empty_address(self):
        """Empty is the signal to draw the initial, so it must not be a URL."""
        self.guild.icon = None
        self.assertEqual(keep_alive._guild_icon(self.guild.id), "")

    def test_an_unknown_server_and_a_bare_instance_do_not_raise(self):
        self.assertEqual(keep_alive._guild_icon(424242), "")
        self.assertEqual(keep_alive._guild_icon(None), "")
        with mock.patch.object(keep_alive, "BOT_INSTANCE", None):
            self.assertEqual(keep_alive._guild_icon(GUILD_ID), "")


class TheFetcher(unittest.TestCase):
    """``fetch_image`` is the only thing here that makes an outbound request."""

    def _fetch(self, url, response=None, error=None):
        target = mock.patch.object(
            discord_auth.urllib.request, "urlopen", side_effect=error, return_value=response
        )
        with target as opened:
            result = discord_auth.fetch_image(url)
        return result, (opened.call_args[0][0] if opened.called else None)

    def test_a_discord_picture_comes_back_with_its_type(self):
        result, _ = self._fetch(
            "https://cdn.discordapp.com/avatars/1/a.png", _Response(b"\x89PNG data")
        )
        self.assertEqual(result, (b"\x89PNG data", "image/png"))

    def test_any_other_host_is_refused_before_a_socket_is_opened(self):
        """The guard that keeps this route from being a request forger.

        Every one of these would otherwise be an outbound request to an address a
        caller chose: the metadata service, a file on disk, an intranet host.
        """
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "https://cdn.discordapp.com.evil.test/a.png",
            "file:///etc/passwd",
            "https://evil.test/a.png",
            "//cdn.discordapp.com/a.png",
            "",
            None,
            42,
        ):
            result, opened = self._fetch(url, _Response())
            self.assertIsNone(result, f"{url!r} was fetched")
            self.assertIsNone(opened, f"{url!r} reached urlopen")

    def test_something_that_is_not_a_picture_is_not_served_as_one(self):
        """A redirect to an HTML error page must not become an ``<img>``."""
        result, _ = self._fetch(
            "https://cdn.discordapp.com/a.png", _Response(b"<html>", content_type="text/html")
        )
        self.assertIsNone(result)

    def test_a_body_past_the_bound_is_refused_not_truncated(self):
        big = b"x" * (discord_auth.IMAGE_MAX_BYTES + 1)
        result, _ = self._fetch("https://cdn.discordapp.com/a.png", _Response(big))
        self.assertIsNone(result)

    def test_an_empty_or_failed_answer_is_not_a_picture(self):
        for response, error in (
            (_Response(b""), None),
            (None, discord_auth.urllib.error.URLError("down")),
            (None, OSError("no route")),
            (_Response(b"x", status=404), None),
        ):
            result, _ = self._fetch("https://cdn.discordapp.com/a.png", response, error)
            self.assertIsNone(result)


class TheImageRoute(_DashboardFixture):
    """``/api/image`` answers for the pictures this session may already see."""

    def setUp(self):
        super().setUp()
        self.guild.icon = _Icon()
        self.other.icon = _Icon("otherhash", self.other.id)

    def _fetched_url(self, **query):
        seen = []

        def record(url):
            seen.append(url)
            return (b"\x89PNG", "image/png")

        with mock.patch.object(keep_alive.discord_auth, "fetch_image", side_effect=record):
            response = self.client.get("/api/image", query_string=query)
        return response, (seen[0] if seen else None)

    def signed_in(self, **kwargs):
        self.sign_in(**kwargs)

    # ── it must not be a way in ──────────────────────────────────────────
    def test_a_request_with_no_session_gets_nothing(self):
        response = self.client.get("/api/image", query_string={"kind": "bot"})
        self.assertEqual(response.status_code, 401)

    def test_a_name_that_is_not_a_picture_gets_nothing(self):
        self.sign_in()
        for kind in ("", "avatar", "banner", "../../etc/passwd", "guild-icon"):
            response, fetched = self._fetched_url(kind=kind, guild_id=GUILD_ID)
            self.assertEqual(response.status_code, 404, kind)
            self.assertIsNone(fetched, f"{kind!r} reached the fetcher")

    def test_a_guild_scoped_session_cannot_reach_another_servers_icon(self):
        """Otherwise the route enumerates the bot's servers one picture at a time.

        The assertion is on what was *fetched*, not only on the status: a scoped
        session quietly getting its own icon back for somebody else's id would
        pass a status check while proving nothing.
        """
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response, fetched = self._fetched_url(kind="guild", guild_id=self.other.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/{GUILD_ID}/", fetched, "the session's own icon is the only one")
        self.assertNotIn(str(self.other.id), fetched)

    def test_a_server_the_bot_is_not_in_gets_nothing(self):
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response, fetched = self._fetched_url(kind="guild", guild_id=424242)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/{GUILD_ID}/", fetched)

    def test_the_operator_may_see_any_servers_icon(self):
        self.sign_in(scope="global")
        response, fetched = self._fetched_url(kind="guild", guild_id=self.other.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(self.other.id), fetched)

    # ── it must serve the picture when there is one ──────────────────────
    def test_a_server_picture_is_returned_as_an_image(self):
        """Mocked, deliberately: a test that needs Discord to be up tests Discord."""
        self.sign_in()
        response, fetched = self._fetched_url(kind="guild", guild_id=GUILD_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.data, b"\x89PNG")
        self.assertIn(f"/{GUILD_ID}/", fetched)
        # Private: the answer depends on who is asking.
        self.assertIn("private", response.headers.get("Cache-Control", ""))

    def test_a_picture_discord_will_not_give_is_a_404_not_an_error(self):
        """A 404 is the normal \"draw the letter\" answer, so it must not be a 500."""
        self.sign_in()
        with mock.patch.object(keep_alive.discord_auth, "fetch_image", return_value=None):
            response = self.client.get(
                "/api/image", query_string={"kind": "guild", "guild_id": GUILD_ID}
            )
        self.assertEqual(response.status_code, 404)

    def test_the_bot_avatar_with_nothing_cached_yet_is_a_404(self):
        self.sign_in()
        with mock.patch.object(keep_alive, "_BOT_AVATAR_URL", None):
            response = self.client.get("/api/image", query_string={"kind": "bot"})
        self.assertEqual(response.status_code, 404)

    def test_the_bot_avatar_is_served_from_the_address_cached_at_boot(self):
        self.sign_in()
        with mock.patch.object(
            keep_alive, "_BOT_AVATAR_URL", "https://cdn.discordapp.com/avatars/999/bot.png"
        ):
            response, fetched = self._fetched_url(kind="bot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetched, "https://cdn.discordapp.com/avatars/999/bot.png")

    def test_a_signed_in_person_with_no_picture_is_a_404(self):
        self.sign_in()
        with self.client.session_transaction() as session:
            session["discord_user"] = {"name": "raj", "avatar": "", "avatar_cdn": ""}
        response = self.client.get("/api/image", query_string={"kind": "me"})
        self.assertEqual(response.status_code, 404)


class ThePolicyWasNotWidened(_DashboardFixture):
    """The tempting fix is a wider ``img-src``. This pins that we did not."""

    def test_images_are_still_only_allowed_from_this_origin(self):
        policy = self.client.get("/login").headers.get("Content-Security-Policy", "")
        self.assertIn("img-src 'self' data:", policy)
        self.assertNotIn("cdn.discordapp.com", policy)
        self.assertNotIn("img-src *", policy)

    def test_no_template_hands_the_browser_a_discord_picture_to_fetch(self):
        """Whatever the policy is, a template must not emit a blocked address.

        The route exists so the pages do not need one; if a future page adds a
        hot-linked avatar it renders as a broken image and nothing else complains,
        which is exactly how the first version of the fallback failed.
        """
        import os
        import re

        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
        )
        offenders = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if re.search(r"cdn\.discordapp\.com", line):
                        offenders.append(f"{name}:{number}")
        self.assertEqual(offenders, [], "a template still hot-links Discord's CDN")


if __name__ == "__main__":
    unittest.main()
