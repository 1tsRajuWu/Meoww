"""Tests for the upload utility functions."""
import os
import tempfile
import unittest
from unittest.mock import patch

# --- Video detection heuristic ---
from cogs.uploads import (
    _is_video,
    _service_order,
    build_info_embed,
    VIDEO_MIMES,
    VIDEO_EXTENSIONS,
)


class TestIsVideo(unittest.TestCase):
    """The bot must route videos to Streamable and everything else to catbox."""

    def test_mp4_mime(self):
        self.assertTrue(_is_video("clip.mp4", "video/mp4"))

    def test_webm_mime(self):
        self.assertTrue(_is_video("clip.webm", "video/webm"))

    def test_mov_mime(self):
        self.assertTrue(_is_video("clip.mov", "video/quicktime"))

    def test_text_not_video(self):
        self.assertFalse(_is_video("readme.txt", "text/plain"))

    def test_image_not_video(self):
        self.assertFalse(_is_video("photo.jpg", "image/jpeg"))

    def test_extension_fallback_mp4(self):
        self.assertTrue(_is_video("clip.mp4", None))

    def test_extension_fallback_mkv(self):
        self.assertTrue(_is_video("clip.mkv", None))

    def test_extension_fallback_no_ext(self):
        self.assertFalse(_is_video("noext", None))

    def test_extension_fallback_txt(self):
        self.assertFalse(_is_video("notes.txt", None))

    def test_content_type_with_charset(self):
        self.assertTrue(_is_video("clip.mp4", "video/mp4; charset=utf-8"))

    def test_avi_video(self):
        self.assertTrue(_is_video("old.avi", "video/x-msvideo"))


# --- filehost formatting ---
from utils.filehost import format_size


class TestFormatSize(unittest.TestCase):
    """Human-readable file sizes."""

    def test_bytes(self):
        self.assertEqual(format_size(500), "500 B")

    def test_kilobytes(self):
        self.assertIn("KB", format_size(5_000))

    def test_megabytes(self):
        self.assertIn("MB", format_size(5_000_000))

    def test_gigabytes(self):
        self.assertIn("GB", format_size(5_000_000_000))


# --- Streamable availability ---
from utils import streamable


class TestStreamableAvailability(unittest.TestCase):
    """Streamable is available only when credentials are set."""

    def test_available_with_creds(self):
        with patch.dict(os.environ, {
            "STREAMABLE_USERNAME": "user",
            "STREAMABLE_PASSWORD": "pass",
        }):
            # Re-read after patching
            streamable._USERNAME = "user"
            streamable._PASSWORD = "pass"
            self.assertTrue(streamable.available())

    def test_not_available_without_creds(self):
        streamable._USERNAME = ""
        streamable._PASSWORD = ""
        self.assertFalse(streamable.available())


# --- Feature registry ---
from utils.features import FEATURES


class TestUploadFeatureRegistered(unittest.TestCase):
    """The upload feature must exist in the registry."""

    def test_feature_exists(self):
        ids = {f.id for f in FEATURES}
        self.assertIn("uploads", ids)

    def test_cog_path(self):
        uploads_feature = next(f for f in FEATURES if f.id == "uploads")
        self.assertEqual(uploads_feature.cog, "cogs.uploads")

    def test_gate_commands(self):
        uploads_feature = next(f for f in FEATURES if f.id == "uploads")
        self.assertIn("upload", uploads_feature.gate_commands)


# --- Permission declarations ---
from utils.permissions import COMMAND_ACCESS
from utils.permissions import EVERYONE, ADMIN


class TestUploadPermissions(unittest.TestCase):
    """Upload commands have the right audience declarations."""

    def test_upload_is_everyone(self):
        self.assertEqual(COMMAND_ACCESS.get("upload"), EVERYONE)

    def test_pin_upload_info_is_admin(self):
        self.assertEqual(COMMAND_ACCESS.get("pin-upload-info"), ADMIN)


# --- catbox client (offline) ---
from utils import catbox


class TestCatboxGuards(unittest.TestCase):
    """catbox must reject what it cannot host, without hitting the network."""

    def test_limit_is_200_mb(self):
        self.assertEqual(catbox.MAX_SIZE, 200 * 1024 * 1024)

    def test_empty_file_rejected(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            path = fh.name
        try:
            self.assertIsNone(catbox.upload(path, "empty.bin"))
        finally:
            os.unlink(path)

    def test_oversize_file_rejected(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.truncate(catbox.MAX_SIZE + 1)
            path = fh.name
        try:
            self.assertIsNone(catbox.upload(path, "big.bin"))
        finally:
            os.unlink(path)

    def test_missing_file_rejected(self):
        self.assertIsNone(catbox.upload("/nonexistent/meow.bin", "meow.bin"))

    def test_filename_is_sanitised(self):
        safe = catbox._safe_filename('weird "name"; evil\r\n.txt')
        self.assertNotIn('"', safe)
        self.assertNotIn("\r", safe)
        self.assertNotIn("\n", safe)

    def test_filename_keeps_extension(self):
        self.assertTrue(catbox._safe_filename("clip.mp4").endswith(".mp4"))

    def test_body_length_matches_streamed_bytes(self):
        """Content-Length must equal the bytes actually sent, or catbox 400s."""
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.write(b"x" * 4096)
            path = fh.name
        try:
            body, length = catbox._body_parts(path, "meow.bin", "----boundary", 4096)
            sent = b"".join(body)
            self.assertEqual(length, len(sent))
            self.assertIn(b"x" * 4096, sent)
        finally:
            os.unlink(path)


class TestServiceOrder(unittest.TestCase):
    """Auto mode uploads for real first and keeps file.kiwi as the last resort."""

    def test_auto_ends_with_filekiwi(self):
        self.assertEqual(_service_order("auto", False)[-1], "filekiwi")

    def test_auto_starts_with_catbox_for_plain_files(self):
        self.assertEqual(_service_order("auto", False)[0], "catbox")

    def test_auto_prefers_streamable_for_video_with_creds(self):
        with patch.object(streamable, "available", return_value=True):
            self.assertEqual(_service_order("auto", True)[0], "streamable")

    def test_auto_skips_streamable_without_creds(self):
        with patch.object(streamable, "available", return_value=False):
            self.assertNotIn("streamable", _service_order("auto", True))

    def test_filekiwi_choice_is_only_filekiwi(self):
        self.assertEqual(_service_order("filekiwi", False), ["filekiwi"])

    def test_unknown_service_falls_back_to_auto_chain(self):
        self.assertEqual(_service_order("nonsense", False)[0], "catbox")


from cogs.uploads import (
    CONTENT_MAX,
    EMBED_TITLE_MAX,
    THREAD_NAME_MAX,
    _fit,
    _label,
    _reason,
    _units,
    build_failure_text,
)


class TestLabelFitsDiscord(unittest.TestCase):
    """Discord rejects a title over 256 chars *after* the file is uploaded.

    The member has already spent the bandwidth at that point, so an
    over-long title (or attachment filename) must not be able to stop the
    link from arriving.
    """

    def test_long_title_is_truncated(self):
        self.assertLessEqual(len(_label("📎 ", "A" * 400)), EMBED_TITLE_MAX)

    def test_long_filename_is_truncated(self):
        self.assertLessEqual(
            len(_label("📎 ", "z" * 300 + ".png")), EMBED_TITLE_MAX,
        )

    def test_exactly_at_the_limit_is_kept(self):
        text = "B" * (EMBED_TITLE_MAX - _units("📎 "))
        self.assertEqual(_label("📎 ", text), "📎 " + text)

    def test_short_title_is_untouched(self):
        self.assertEqual(_label("📎 ", "My clip"), "📎 My clip")

    def test_measured_the_way_discord_measures(self):
        """A 256-character title holding one emoji is 257 units to Discord."""
        self.assertEqual(_units("📎 "), 3)
        self.assertEqual(len("📎 "), 2)

    def test_emoji_heavy_title_still_fits(self):
        """The boundary is units, not code points: 400 emoji would overflow."""
        label = _label("📎 ", "🎬" * 400)
        self.assertLessEqual(_units(label), EMBED_TITLE_MAX)

    def test_emoji_heavy_filename_still_fits(self):
        self.assertLessEqual(_units(_label("📁 ", "🎬" * 400)), EMBED_TITLE_MAX)

    def test_a_single_emoji_is_never_cut_in_half(self):
        """Trimming mid-surrogate would produce a replacement character."""
        out = _fit("🎬" * 10, 5)
        self.assertNotIn("\ufffd", out)
        self.assertLessEqual(_units(out), 5)

    def test_thread_name_fits_its_own_limit(self):
        name = _fit(_label("📎 ", "🎬" * 400), THREAD_NAME_MAX)
        self.assertLessEqual(_units(name), THREAD_NAME_MAX)

    def test_fit_leaves_a_short_string_alone(self):
        self.assertEqual(_fit("hello", 100), "hello")

    def test_fit_still_returns_something_for_a_tiny_limit(self):
        self.assertTrue(_fit("hello", 1))

    def test_whitespace_only_gets_a_placeholder(self):
        self.assertTrue(_label("📎 ", "   ").strip())

    def test_markdown_newlines_are_collapsed(self):
        self.assertNotIn("\n", _label("📎 ", "a\nb"))


class TestFailureCard(unittest.TestCase):
    """The explanation has to fit, or the follow-up is rejected too."""

    def test_fits_discord_content_limit(self):
        errors = [f"catbox: {'E' * 5000}", f"file.kiwi: {'F' * 5000}"]
        self.assertLessEqual(_units(build_failure_text(errors)), CONTENT_MAX)

    def test_fits_with_an_emoji_heavy_reason(self):
        errors = [f"catbox: {'🔥' * 4000}"]
        self.assertLessEqual(_units(build_failure_text(errors)), CONTENT_MAX)

    def test_names_every_host_that_was_tried(self):
        text = build_failure_text(["catbox: nope", "file.kiwi: nope"])
        self.assertIn("catbox", text)
        self.assertIn("file.kiwi", text)

    def test_says_something_when_there_is_no_detail(self):
        self.assertIn("refused", build_failure_text([]))

    def test_reason_is_one_bounded_line(self):
        self.assertLessEqual(len(_reason(RuntimeError("x\n" + "y" * 5000))), 300)
        self.assertNotIn("\n", _reason(RuntimeError("a\nb")))

    def test_reason_handles_an_exception_with_no_message(self):
        self.assertTrue(_reason(RuntimeError()))


class TestInfoEmbed(unittest.TestCase):
    """The pinned/sticky instructions must match what the bot really does."""

    def test_mentions_catbox(self):
        self.assertIn("catbox", build_info_embed().description.lower())

    def test_does_not_advertise_file_io(self):
        self.assertNotIn("file.io", build_info_embed().description.lower())

    def test_keeps_title_used_by_startup_check(self):
        self.assertEqual(build_info_embed().title, "📎 How to Upload Big Files")

    def test_fits_discords_embed_limits(self):
        """The sticky post is pinned on every boot; a rejected embed is a silent
        failure, since the only symptom is that nothing appears."""
        embed = build_info_embed()
        self.assertLessEqual(_units(embed.title or ""), EMBED_TITLE_MAX)
        self.assertLessEqual(_units(embed.description or ""), 4096)
        self.assertLessEqual(_units(embed.footer.text or ""), 2048)
        self.assertLessEqual(len(embed.fields), 25)


if __name__ == "__main__":
    unittest.main()
