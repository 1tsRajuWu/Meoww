"""The voice capability gate: what is required, what is an upgrade, and what says so.

This module exists because four separate things have to be true before a frame
reaches Discord, they fail in four different ways, and none of them used to say
anything to the member — the card said "playing", the bot was in the channel, and
nothing was heard. The tests below are mostly about the *distinctions*, because
getting one wrong is how a working host gets told it is broken:

* **``davey`` is required**, and that is not a style preference: ``discord.py``
  refuses to construct a voice client without it (Discord's end-to-end
  encryption is mandatory for a bot now), so an absent one means *no connection*.
* **ffmpeg is required**, and a host with none on ``PATH`` still has one, because
  ``imageio-ffmpeg`` ships a static build. That fallback is what makes this work
  on a host that cannot install packages, so it is pinned.
* **libopus is not required to play** — an Opus stream is passed through and
  ``discord.py`` never builds an encoder for it — and it *is* what makes a live
  volume possible. It is reported as an upgrade rather than as a fault, because
  the alternative was demanding a Docker host for a feature that does not need one.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from utils import voice_caps


def _caps(**overrides):
    base = dict(
        ffmpeg="/usr/bin/ffmpeg",
        ffmpeg_source="PATH",
        opus=True,
        opus_source="/usr/lib/libopus.so.0",
        dave=True,
        dave_version="0.1.6",
        dave_problem=None,
    )
    base.update(overrides)
    return voice_caps.Caps(**base)


class WhatIsRequired(unittest.TestCase):
    def test_a_complete_host_can_play(self):
        caps = _caps()

        self.assertTrue(caps.playable)
        self.assertTrue(caps.live_volume)
        self.assertEqual(caps.missing(), ())

    def test_without_davey_it_cannot_play_at_all(self):
        caps = _caps(dave=False, dave_version=None, dave_problem="davey is not importable")

        self.assertFalse(caps.playable)
        self.assertIn("davey", caps.missing()[0])
        self.assertIn("pip install davey", caps.missing()[0])

    def test_without_ffmpeg_it_cannot_play(self):
        caps = _caps(ffmpeg=None, ffmpeg_source="not found")

        self.assertFalse(caps.playable)
        self.assertIn("ffmpeg", " ".join(caps.missing()))
        self.assertIn("imageio-ffmpeg", " ".join(caps.missing()))

    def test_without_libopus_it_still_plays(self):
        # The load-bearing distinction: no live volume is a downgrade, not a fault.
        caps = _caps(opus=False, opus_source="not found")

        self.assertTrue(caps.playable)
        self.assertFalse(caps.live_volume)
        self.assertEqual(caps.missing(), ())
        self.assertIn("no live volume", caps.quality)


class TheLineSaysTheAnswer(unittest.TestCase):
    def test_a_working_host_reads_as_working(self):
        line = _caps().line()

        self.assertIn("can play", line)
        self.assertIn("host-installed", line)
        self.assertIn("0.1.6", line)

    def test_a_broken_host_names_every_reason(self):
        line = _caps(ffmpeg=None, dave=False, dave_version=None).line()

        self.assertIn("cannot play", line)
        self.assertIn("davey", line)
        self.assertIn("ffmpeg", line)

    def test_the_dict_is_what_the_console_reads(self):
        payload = _caps(opus=False, opus_source="not found").to_dict()

        self.assertTrue(payload["playable"])
        self.assertFalse(payload["live_volume"])
        self.assertEqual(payload["opus_source"], "not found")
        self.assertEqual(payload["missing"], [])


class FindingFfmpeg(unittest.TestCase):
    def test_a_system_binary_wins(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(voice_caps.shutil, "which", return_value="/usr/bin/ffmpeg"):
            path, source = voice_caps.ffmpeg_path()

        self.assertEqual((path, source), ("/usr/bin/ffmpeg", "PATH"))

    def test_the_bundled_binary_is_the_fallback(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(voice_caps.shutil, "which", return_value=None), \
             mock.patch.object(voice_caps.os.path, "exists", return_value=True):
            fake = SimpleNamespace(get_ffmpeg_exe=lambda: "/venv/imageio_ffmpeg/binaries/ffmpeg")
            with mock.patch.dict("sys.modules", {"imageio_ffmpeg": fake}):
                path, source = voice_caps.ffmpeg_path()

        self.assertEqual(path, "/venv/imageio_ffmpeg/binaries/ffmpeg")
        self.assertEqual(source, "imageio-ffmpeg")

    def test_an_explicit_override_is_believed(self):
        with mock.patch.dict("os.environ", {"MEOW_FFMPEG": "/opt/ffmpeg"}, clear=True), \
             mock.patch.object(voice_caps.os.path, "exists", return_value=True):
            path, source = voice_caps.ffmpeg_path()

        self.assertEqual((path, source), ("/opt/ffmpeg", "MEOW_FFMPEG"))

    def test_an_override_that_is_not_a_file_is_reported_as_such(self):
        with mock.patch.dict("os.environ", {"MEOW_FFMPEG": "/opt/nope"}, clear=True), \
             mock.patch.object(voice_caps.os.path, "exists", return_value=False):
            path, source = voice_caps.ffmpeg_path()

        self.assertIsNone(path)
        self.assertIn("not a file", source)

    def test_nothing_anywhere_is_reported_not_guessed(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(voice_caps.shutil, "which", return_value=None), \
             mock.patch.dict("sys.modules", {"imageio_ffmpeg": None}):
            path, source = voice_caps.ffmpeg_path()

        self.assertIsNone(path)
        # Whichever way it failed, the answer names the thing that failed rather
        # than claiming there is no ffmpeg anywhere.
        self.assertTrue("imageio-ffmpeg" in source or "not found" in source)


class FindingOpus(unittest.TestCase):
    def test_the_vendored_directory_is_offered_first(self):
        candidates = voice_caps.opus_candidates()

        self.assertTrue(
            any(voice_caps.VENDORED_DIR in path for path in candidates),
            "a repository that ships its own library must be believed before the system",
        )

    def test_an_unloadable_library_is_not_a_crash(self):
        with mock.patch.object(voice_caps, "opus_candidates", return_value=["/nope/libopus.so"]), \
             mock.patch.object(voice_caps.os.path, "exists", return_value=False):
            loaded, where = voice_caps.load_opus()

        self.assertFalse(loaded)
        self.assertEqual(where, "not found")


class TheProbeIsAskedOnce(unittest.TestCase):
    def test_the_cache_is_reused_and_refresh_ignores_it(self):
        with mock.patch.object(voice_caps, "_cache", None), \
             mock.patch.object(voice_caps, "ffmpeg_path", return_value=("/usr/bin/ffmpeg", "PATH")) as which, \
             mock.patch.object(voice_caps, "load_opus", return_value=(True, "default")), \
             mock.patch.object(voice_caps, "_dave", return_value=(True, "1", None)):
            voice_caps.probe()
            voice_caps.probe()
            self.assertEqual(which.call_count, 1, "the probe re-ran for the same answer")

            voice_caps.probe(refresh=True)
            self.assertEqual(which.call_count, 2)

        voice_caps._cache = None


class TheGateSpeaksBeforeAnythingIsAttempted(unittest.TestCase):
    def test_a_playable_host_is_let_through_silently(self):
        ok, why = voice_caps.build_source(_caps())

        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_an_unplayable_host_gets_a_sentence_with_the_fix(self):
        ok, why = voice_caps.build_source(_caps(ffmpeg=None, dave=False, dave_version=None))

        self.assertFalse(ok)
        self.assertIn("cannot play", why)
        self.assertIn("pip install davey", why)


if __name__ == "__main__":
    unittest.main()
