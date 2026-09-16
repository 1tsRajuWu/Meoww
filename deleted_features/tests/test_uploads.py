"""Tests for the upload utility functions."""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
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


from utils import db as uploads_db


def _setting(value):
    """A stand-in for ``utils.db.get_setting`` that always returns ``value``."""
    async def _get(guild_id, key, default=None):
        return value
    return _get


from cogs import uploads
from cogs.uploads import (
    CONTENT_MAX,
    DEFAULT_UPLOAD_CHANNEL_ID,
    EMBED_TITLE_MAX,
    MAX_CONCURRENT_UPLOADS,
    MAX_HOSTED_SIZE,
    THREAD_NAME_MAX,
    Uploads,
    _fit,
    _in_upload_thread,
    _label,
    _reason,
    _units,
    build_failure_text,
    resolve_default_channel_id,
)


class TestUploadCeiling(unittest.TestCase):
    """`/upload` must not refuse what the host would have taken.

    A 123 MB clip was turned away by a flat 100 MB cap and pointed at
    `/upload-big`, so the member had to drag it into an empty file.kiwi folder
    by hand — and then watch file.kiwi fail to preview a 4K video. catbox takes
    200 MB, so the ceiling is the host's, and the two are pinned together here
    so a later edit cannot lower one behind the other's back.
    """

    def test_ceiling_is_what_the_host_actually_takes(self):
        self.assertEqual(MAX_HOSTED_SIZE, catbox.MAX_SIZE)

    def test_the_reported_123_mb_clip_is_accepted(self):
        self.assertLess(123 * 1024 * 1024, MAX_HOSTED_SIZE)

    def test_pinned_instructions_quote_the_real_limit(self):
        limit_mb = MAX_HOSTED_SIZE // (1024 * 1024)
        text = build_info_embed().description or ""
        self.assertIn(f"Bigger than {limit_mb} MB?", text)
        # The stale claim — "**25 MB** (free) / **50 MB** (boosted)" — is what
        # let a member believe their server could not attach the file at all.
        # Only the exact claim is fenced off, so a later table of the real boost
        # tiers (10 / 50 / 100 / 500 MB) is not a test failure.
        self.assertNotIn("**25 MB**", text)
        self.assertNotIn("**50 MB**", text)


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


class TestUploadChannelResolution(unittest.IsolatedAsyncioTestCase):
    """Defaulting the channel to "unset" silently disabled /upload.

    It answered "No upload channel is configured for this server" and posted
    nothing, so every server that had not been configured lost the command.
    The default has to be a real channel, and a stale or foreign setting must
    fall through instead of taking uploads down with it.
    """

    def test_default_is_a_real_channel(self):
        self.assertEqual(DEFAULT_UPLOAD_CHANNEL_ID, 1548959110467289169)
        self.assertTrue(resolve_default_channel_id({}))

    def test_env_overrides_the_default(self):
        self.assertEqual(resolve_default_channel_id({"UPLOAD_CHANNEL_ID": "123"}), 123)

    def test_blank_env_falls_back(self):
        self.assertEqual(resolve_default_channel_id({"UPLOAD_CHANNEL_ID": "  "}),
                         DEFAULT_UPLOAD_CHANNEL_ID)

    def test_a_typo_does_not_crash_the_bot(self):
        self.assertEqual(resolve_default_channel_id({"UPLOAD_CHANNEL_ID": "clips"}),
                         DEFAULT_UPLOAD_CHANNEL_ID)

    def _cog(self, resolved: dict):
        bot = mock.Mock()
        bot.get_channel.side_effect = lambda cid: resolved.get(cid)

        async def _fetch(cid):
            return resolved.get(cid)

        bot.fetch_channel.side_effect = _fetch
        return Uploads(bot)

    async def test_guild_setting_wins(self):
        mine = mock.Mock(id=1000, guild=mock.Mock(id=555))
        cog = self._cog({1000: mine, DEFAULT_UPLOAD_CHANNEL_ID: mock.Mock()})
        with patch.object(uploads_db, "get_setting", new=_setting("1000")):
            self.assertIs(await cog._upload_channel(mock.Mock(id=555)), mine)

    async def test_no_setting_uses_the_default(self):
        default = mock.Mock(id=DEFAULT_UPLOAD_CHANNEL_ID, guild=mock.Mock(id=555))
        cog = self._cog({DEFAULT_UPLOAD_CHANNEL_ID: default})
        with patch.object(uploads_db, "get_setting", new=_setting(None)):
            self.assertIs(await cog._upload_channel(mock.Mock(id=555)), default)

    async def test_a_stale_setting_falls_through(self):
        default = mock.Mock(id=DEFAULT_UPLOAD_CHANNEL_ID, guild=mock.Mock(id=555))
        cog = self._cog({DEFAULT_UPLOAD_CHANNEL_ID: default})
        with patch.object(uploads_db, "get_setting", new=_setting("424242")):
            self.assertIs(await cog._upload_channel(mock.Mock(id=555)), default)

    async def test_a_channel_from_another_guild_is_refused(self):
        foreign = mock.Mock(id=999, guild=mock.Mock(id=777))
        default = mock.Mock(id=DEFAULT_UPLOAD_CHANNEL_ID, guild=mock.Mock(id=555))
        cog = self._cog({999: foreign, DEFAULT_UPLOAD_CHANNEL_ID: default})
        with patch.object(uploads_db, "get_setting", new=_setting("999")):
            self.assertIs(await cog._upload_channel(mock.Mock(id=555)), default)

    async def test_nothing_resolves_means_nothing(self):
        cog = self._cog({})
        with patch.object(uploads_db, "get_setting", new=_setting(None)):
            self.assertIsNone(await cog._upload_channel(mock.Mock(id=555)))

    async def test_a_garbage_setting_does_not_raise(self):
        default = mock.Mock(id=DEFAULT_UPLOAD_CHANNEL_ID, guild=mock.Mock(id=555))
        cog = self._cog({DEFAULT_UPLOAD_CHANNEL_ID: default})
        with patch.object(uploads_db, "get_setting", new=_setting("not-an-id")):
            self.assertIs(await cog._upload_channel(mock.Mock(id=555)), default)


class TestBigFilesAreUploadedForTheMember(unittest.IsolatedAsyncioTestCase):
    """The reported upload: a 123 MB clip the command refused to touch.

    It hit the flat 100 MB ceiling, was answered with "use /upload-big", and
    became an empty file.kiwi folder the member had to fill in by hand. This
    drives the real command with a 123 MB attachment and asserts the bot
    uploads it and posts a link, which is the whole point of the command.
    """

    def _interaction(self):
        channel = mock.Mock(id=DEFAULT_UPLOAD_CHANNEL_ID, guild=mock.Mock(id=555))
        thread = mock.Mock(send=mock.AsyncMock())
        message = mock.Mock(create_thread=mock.AsyncMock(return_value=thread))
        channel.send = mock.AsyncMock(return_value=message)

        interaction = mock.Mock()
        interaction.guild = mock.Mock(id=555)
        interaction.user = mock.Mock(display_name="R A J", mention="@R A J")
        interaction.response.defer = mock.AsyncMock()
        interaction.response.send_message = mock.AsyncMock()
        interaction.followup.send = mock.AsyncMock()
        return interaction, channel

    def _attachment(self, size: int):
        attachment = mock.Mock(
            filename="12608328_3840_2160_30fps.mp4",
            size=size,
            content_type="video/mp4",
        )

        async def _save(path):
            with open(path, "wb") as handle:
                handle.write(b"x" * 2048)

        attachment.save = _save
        return attachment

    async def test_a_123_mb_clip_is_uploaded_not_turned_away(self):
        cog = Uploads(mock.Mock())
        interaction, channel = self._interaction()
        cog._upload_channel = mock.AsyncMock(return_value=channel)

        with patch.object(
            uploads, "_resolve_link",
            return_value=("https://files.catbox.moe/meow.mp4", "catbox", False),
        ):
            await Uploads.upload.callback(
                cog, interaction, self._attachment(123 * 1024 * 1024), "", "auto",
            )

        interaction.response.send_message.assert_not_awaited()
        channel.send.assert_awaited()
        confirmation = interaction.followup.send.await_args.args[0]
        self.assertIn("catbox", confirmation)
        self.assertIn("files.catbox.moe/meow.mp4", confirmation)

    async def test_only_a_file_no_host_can_take_is_refused(self):
        cog = Uploads(mock.Mock())
        interaction, _ = self._interaction()

        await Uploads.upload.callback(
            cog, interaction, self._attachment(MAX_HOSTED_SIZE + 1), "", "auto",
        )

        refusal = interaction.response.send_message.await_args.args[0]
        self.assertIn("upload-big", refusal)
        self.assertIn(str(MAX_HOSTED_SIZE // (1024 * 1024)), refusal)


class TestBoundedUploadThreads(unittest.IsolatedAsyncioTestCase):
    """Uploads share the executor with the database backup loop.

    Each one can hold a worker for up to five minutes, so without a cap a
    burst of uploads queues every backup behind them — the same "progress did
    not save" symptom, arriving through a different door.
    """

    async def test_only_the_cap_runs_at_once(self):
        live = 0
        peak = 0

        def slow():
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            time.sleep(0.05)
            live -= 1
            return True

        results = await asyncio.gather(*(_in_upload_thread(slow) for _ in range(12)))
        self.assertTrue(all(results))
        self.assertLessEqual(peak, MAX_CONCURRENT_UPLOADS)
        self.assertGreaterEqual(peak, 2, "the cap should not serialise uploads")

    def test_the_cap_leaves_room_for_the_backup_loop(self):
        """Uploads must not be able to use up the shared executor."""
        workers = min(32, (os.cpu_count() or 1) + 4)
        self.assertLess(MAX_CONCURRENT_UPLOADS, workers)


class TestTheStickyAndTheCommandAgree(unittest.TestCase):
    """One source of truth for the upload channel.

    The sticky post had its own hardcoded channel id while /upload resolved a
    different one, so the bot could keep advertising a channel it would refuse
    to post in. The sticky now reads the same constant, and asks for the pins
    directly rather than scanning a window that the busy channel had already
    pushed the sticky out of (which posted a second copy on every restart).
    """

    def setUp(self):
        here = Path(__file__).resolve()
        # Works both restored (tests/test_uploads.py -> repo root is
        # parent.parent) and archived
        # (deleted_features/tests/test_uploads.py -> repo root is
        # parent.parent.parent).
        candidates = [here.parent.parent / "bot.py", here.parent.parent.parent / "bot.py"]
        bot_py = next((p for p in candidates if p.exists()), candidates[0])
        self.src = bot_py.read_text(errors="replace")
        if "Auto-post sticky upload info message" not in self.src:
            self.skipTest("bot.py has no upload sticky (feature archived)")
        start = self.src.index("Auto-post sticky upload info message")
        self.block = self.src[start:start + 2000]

    def test_no_second_hardcoded_channel(self):
        self.assertNotIn(str(DEFAULT_UPLOAD_CHANNEL_ID), self.block)

    def test_channels_come_from_the_cog(self):
        self.assertIn("from cogs.uploads import", self.block)
        self.assertIn("UPLOAD_CHANNEL_ID", self.block)

    def test_the_sticky_looks_at_the_pins(self):
        self.assertIn("channel.pins()", self.block)


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
