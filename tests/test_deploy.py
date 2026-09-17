"""Asking Render for a deploy, and what the operator is told either way.

The hook URL is a credential — anyone holding it can start a build — so the two
things pinned here are that an unset hook is reported as *not configured* rather
than as a failure, and that no sentence this module returns can contain the URL,
not even when the exception it caught was quoting it.
"""
import unittest
import urllib.error
from unittest import mock

from utils import deploy

HOOK = "https://api.render.com/deploy/srv-abc?key=super-secret"


class _Response:
    def __init__(self, status=201):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NotConfigured(unittest.TestCase):
    def test_an_unset_hook_is_not_configured(self):
        with mock.patch.dict("os.environ", {deploy.ENV: ""}):
            self.assertFalse(deploy.configured())

    def test_a_hook_must_look_like_a_url(self):
        with mock.patch.dict("os.environ", {deploy.ENV: "srv-abc"}):
            self.assertFalse(deploy.configured(), "a bare id is not something to POST to")

    def test_a_real_hook_is_configured(self):
        with mock.patch.dict("os.environ", {deploy.ENV: HOOK}):
            self.assertTrue(deploy.configured())

    def test_the_message_says_no_deploy_happened(self):
        import asyncio

        with mock.patch.dict("os.environ", {deploy.ENV: ""}):
            accepted, note = asyncio.run(deploy.trigger())
        self.assertFalse(accepted)
        self.assertIn("nothing was deployed", note)


class Triggering(unittest.TestCase):
    def _trigger(self, urlopen):
        import asyncio

        with mock.patch.dict("os.environ", {deploy.ENV: HOOK}), mock.patch.object(
            deploy.urllib.request, "urlopen", urlopen
        ):
            return asyncio.run(deploy.trigger())

    def test_a_deploy_is_one_post_to_the_hook(self):
        seen = {}

        def urlopen(request, timeout=None):
            seen["method"] = request.method
            seen["url"] = request.full_url
            seen["data"] = request.data
            return _Response(201)

        accepted, note = self._trigger(urlopen)
        self.assertTrue(accepted)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], HOOK)
        self.assertIn("accepted", note)

    def test_a_refused_hook_is_reported_with_its_status(self):
        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(HOOK, 401, "Unauthorized", {}, None)

        accepted, note = self._trigger(urlopen)
        self.assertFalse(accepted)
        self.assertIn("401", note)

    def test_a_failure_never_repeats_the_hook(self):
        """A URLError carries the URL in its own message; the sentence must not."""
        def urlopen(request, timeout=None):
            raise urllib.error.URLError(f"<urlopen error timed out for {HOOK}>")

        accepted, note = self._trigger(urlopen)
        self.assertFalse(accepted)
        self.assertNotIn("super-secret", note)
        self.assertNotIn("api.render.com", note)
        self.assertIn("URLError", note, "the failure kind is worth naming")

    def test_a_4xx_without_an_exception_is_not_a_deploy(self):
        accepted, note = self._trigger(lambda request, timeout=None: _Response(404))
        self.assertFalse(accepted)
        self.assertIn("404", note)


if __name__ == "__main__":
    unittest.main()
