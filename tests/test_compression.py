"""The gzip layer, and the answers it is *not* allowed to touch.

Egress is what this host is billed for, so the pages and the market board are
compressed where they are written. That is a change to every response this
process produces, which is exactly the kind of change that needs the boundaries
written down: a body that must stay byte-identical, a length that must describe
what is actually sent, and the two dozen answers (images, redirects, ``304``\\ s,
anything already encoded) that must come out untouched.

The ``inflates_*`` tests are the ones that would catch a real regression: a
compressed body that no longer inflates to the answer it replaced is a page
nobody can read, and it is invisible to a test that only checks the header.
"""
import gzip
import unittest

from flask import Response

import keep_alive
from utils import compression


def _response(body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200) -> Response:
    return Response(body, status=status, content_type=content_type)


class WhatAMessageSaysItTakes(unittest.TestCase):
    """``Accept-Encoding`` parsing, including the ways it says no."""

    def test_plain_gzip_is_a_yes(self):
        self.assertTrue(compression.accepts_gzip("gzip"))
        self.assertTrue(compression.accepts_gzip("gzip, deflate, br"))
        self.assertTrue(compression.accepts_gzip("br, gzip;q=0.8"))

    def test_a_missing_header_is_a_no(self):
        self.assertFalse(compression.accepts_gzip(""))
        self.assertFalse(compression.accepts_gzip(None))
        self.assertFalse(compression.accepts_gzip("br, deflate"))

    def test_q_zero_is_a_refusal_not_a_sighting(self):
        """A client asking for ``gzip;q=0`` is asking for everything but gzip."""
        self.assertFalse(compression.accepts_gzip("gzip;q=0"))
        self.assertFalse(compression.accepts_gzip("gzip;q=0.0, br"))

    def test_a_wildcard_covers_gzip_but_an_explicit_refusal_beats_it(self):
        self.assertTrue(compression.accepts_gzip("*"))
        self.assertFalse(compression.accepts_gzip("*, gzip;q=0"))


class WhatCountsAsText(unittest.TestCase):
    """Which content types shrink, and which only get bigger."""

    def test_the_written_answers_are_text(self):
        for content_type in (
            "text/html; charset=utf-8",
            "text/css",
            "application/json",
            "application/javascript; charset=utf-8",
            "image/svg+xml",
        ):
            self.assertTrue(compression.is_compressible(content_type), content_type)

    def test_pictures_and_octets_are_not(self):
        for content_type in ("image/png", "image/jpeg", "application/octet-stream", "font/woff2", ""):
            self.assertFalse(compression.is_compressible(content_type), content_type)


class TheRulesOfTheDoor(unittest.TestCase):
    """Each branch, and the answer it protects."""

    def test_a_big_text_answer_is_packed(self):
        response = _response(b"<p>hello</p>" * 200)
        self.assertTrue(compression.compress(response))
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(response.get_data()), b"<p>hello</p>" * 200)

    def test_the_length_reported_is_the_length_sent(self):
        """A ``Content-Length`` describing the *uncompressed* body is a lie."""
        body = b"<p>hello</p>" * 200
        response = _response(body)
        self.assertTrue(compression.compress(response))
        self.assertEqual(int(response.headers["Content-Length"]), len(response.get_data()))
        self.assertLess(int(response.headers["Content-Length"]), len(body))

    def test_a_small_answer_is_left_alone(self):
        response = _response(b"<p>hello</p>")
        self.assertFalse(compression.compress(response))
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertIn(f"under {compression.MIN_BYTES} bytes", compression.skip_reason(response))

    def test_a_picture_is_left_alone(self):
        response = _response(b"\x89PNG\r\n\x1a\n" + b"0" * 5000, "image/png")
        self.assertFalse(compression.compress(response))
        self.assertEqual(compression.skip_reason(response), "a body that is not text")

    def test_an_answer_that_says_it_is_already_encoded_is_trusted(self):
        response = _response(b"x" * 5000)
        response.headers["Content-Encoding"] = "br"
        self.assertFalse(compression.compress(response))
        self.assertEqual(compression.skip_reason(response), "an answer that is already encoded")

    def test_a_304_has_no_body_to_pack(self):
        response = _response(b"", status=304)
        self.assertFalse(compression.compress(response))
        self.assertEqual(compression.skip_reason(response), "an answer that has no body")

    def test_a_body_being_produced_is_not_read_to_pack_it(self):
        response = Response(iter([b"a" * 5000]), content_type="text/html")
        self.assertFalse(compression.compress(response))
        self.assertEqual(compression.skip_reason(response), "a body that is still being produced")

    def test_an_existing_vary_keeps_what_it_had(self):
        """The market endpoints vary by ``Origin``; that is somebody's allowance."""
        response = _response(b"x" * 5000)
        response.headers["Vary"] = "Origin"
        self.assertTrue(compression.compress(response))
        self.assertEqual(response.headers["Vary"], "Origin, Accept-Encoding")

    def test_a_body_that_is_already_gzipped_does_not_get_double_gzipped(self):
        """``skip_reason`` is what the wrapper consults, so it has to be enough."""
        once = _response(b"<p>hello</p>" * 200)
        self.assertTrue(compression.compress(once))
        self.assertFalse(compression.compress(once))
        self.assertEqual(gzip.decompress(once.get_data()), b"<p>hello</p>" * 200)


class TheAppItself(unittest.TestCase):
    """The layer wired into the real app, over real routes."""

    def setUp(self):
        self.client = keep_alive.app.test_client()

    def _get(self, path, *, accept="gzip"):
        headers = {"Accept-Encoding": accept} if accept else {}
        return self.client.get(path, headers=headers)

    def test_the_live_board_is_packed_and_still_says_the_same_thing(self):
        plain = self.client.get("/api/market/quotes")
        packed = self._get("/api/market/quotes")
        self.assertEqual(packed.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(packed.get_data()), plain.get_data())
        self.assertLess(len(packed.get_data()), len(plain.get_data()))
        self.assertIn("Accept-Encoding", packed.headers["Vary"])

    def test_a_client_that_takes_no_gzip_still_reads_the_page(self):
        """The layer must be invisible to everything that does not opt in."""
        plain = self.client.get("/api/market/quotes")
        self.assertNotIn("Content-Encoding", plain.headers)
        self.assertEqual(plain.status_code, 200)

    def test_the_rendered_pages_shrink_by_a_factor_worth_having(self):
        for path in ("/", "/systems", "/login"):
            with self.subTest(path=path):
                plain = self.client.get(path)
                packed = self._get(path)
                self.assertEqual(packed.headers.get("Content-Encoding"), "gzip", path)
                self.assertEqual(gzip.decompress(packed.get_data()), plain.get_data(), path)
                self.assertLess(len(packed.get_data()) * 2, len(plain.get_data()), path)

    def test_the_security_pass_still_ran_on_a_packed_answer(self):
        """Compression is an ``after_request`` hook too, so the order is a fact."""
        packed = self._get("/")
        self.assertEqual(packed.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("Content-Security-Policy", packed.headers)
        self.assertIn("Content-Encoding", packed.headers)

    def test_a_picture_from_the_origin_is_left_byte_for_byte(self):
        plain = self.client.get("/static/logo.svg")
        packed = self._get("/static/logo.svg")
        self.assertEqual(packed.status_code, plain.status_code)
        self.assertEqual(packed.get_data(), plain.get_data())


if __name__ == "__main__":
    unittest.main()
