"""Tests for the upload utility functions."""
import os
import tempfile
import unittest
from unittest.mock import patch

# --- Video detection heuristic ---
from cogs.uploads import _is_video, VIDEO_MIMES, VIDEO_EXTENSIONS


class TestIsVideo(unittest.TestCase):
    """The bot must route videos to Streamable and everything else to file.io."""

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


if __name__ == "__main__":
    unittest.main()
