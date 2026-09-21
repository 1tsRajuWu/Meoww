"""Music, tested without a Discord connection: the player's two silent failures.

Both of these were *shipped* bugs, and neither had a symptom that pointed at
itself.

**A deliberate stop is not a finished song.** Applying a filter or changing the
volume replaces the audio source, and the old source's after-callback fires on
the way out — so the queue advanced too, and changing a filter ate a queued
track. A generation counter fixes it: every source's callback is tagged with the
generation it belongs to, and a callback only advances the queue while its
generation is current.

**A stream that dies is not a short song.** ffmpeg exiting early arrives as
``err=None``, exactly like a track finishing — which is what "the music is
short" was. The player knows how long the source should have run, and a source
that ends far short of that is reported as a fault.

ffmpeg and yt-dlp are never invoked here: the source classes are mocked, the
resolver is never called, and ``play_next``'s network half is replaced with a
recorder.
"""
import asyncio
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from utils import player as player_module
from utils.media_source import Track

GUILD = 321


def _track(title: str, **kwargs) -> Track:
    """A track with sensible defaults; ``kwargs`` wins, including ``duration``."""
    fields = {
        "title": title,
        "provider": "youtube",
        "video_id": "abc123",
        "stream_url": "http://example.invalid/stream",
        "duration": 180,
        "requester": "Raj",
    }
    fields.update(kwargs)
    return Track(**fields)


class _FakeChannel:
    def __init__(self, cid: int = 99):
        self.id = cid
        self.name = "voice"
        self.bitrate = 128000
        self.members = []
        self.guild = SimpleNamespace(id=GUILD)
        self.mention = f"<#{cid}>"


class _FakeVoiceClient:
    """Mimics the parts of VoiceClient the player touches.

    ``stop()`` invokes the registered after-callback, the same way discord.py's
    audio thread does when a source is stopped or finishes.
    """

    def __init__(self):
        self.channel = _FakeChannel()
        self.after = None
        self.played = []
        self.stop_calls = 0
        self._playing = False
        self._paused = False

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._paused

    def play(self, source, after=None):
        self.played.append(source)
        self.after = after
        self._playing = True
        self._paused = False

    def stop(self):
        self.stop_calls += 1
        self._playing = False
        self._paused = False
        callback, self.after = self.after, None
        if callback is not None:
            callback(None)


class _PlayerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        self.player = player_module.GuildPlayer(self.bot, GUILD)
        self.vc = _FakeVoiceClient()
        self.player.voice_client = self.vc

    async def asyncTearDown(self):
        player_module.PLAYERS.pop(GUILD, None)

    def _recorder(self):
        """Replace play_next with something that records and mimics the pop."""
        calls = []

        async def fake_play_next(error=None):
            calls.append(error)
            if self.player.queue:
                self.player.current = self.player.queue.pop(0)

        self.player.play_next = fake_play_next
        return calls


class FilterRestartKeepsTheQueue(_PlayerCase):
    async def test_restarting_for_a_filter_does_not_advance_the_queue(self):
        self.player.current = _track("Now Playing")
        self.player.queue = [_track("Queued Song")]
        self.vc.play(object(), after=self.player._after_for(self.player._generation))
        calls = self._recorder()

        self.assertTrue(self.player.restart_current())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(calls, [], "the deliberate stop was treated as a finished song")
        self.assertEqual(len(self.player.queue), 1, "a queued track was consumed by the restart")
        self.assertEqual(self.player.current.title, "Now Playing")
        self.assertEqual(self.vc.stop_calls, 1)

    async def test_the_replacement_source_still_advances_when_it_really_ends(self):
        self.player.current = _track("Now Playing")
        self.player.queue = [_track("Queued Song")]
        self.vc.play(object(), after=self.player._after_for(self.player._generation))
        calls = self._recorder()

        self.player.restart_current()
        await asyncio.sleep(0)
        self.assertEqual(calls, [])

        self.vc.after(None)          # the re-rendered track finishes for real
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(len(calls), 1, "the queue stalled after a restart")
        self.assertEqual(self.player.current.title, "Queued Song")

    async def test_a_stale_callback_from_a_replaced_source_is_ignored(self):
        self.player.current = _track("Now Playing")
        stale = self.player._after_for(self.player._generation)
        self.vc.play(object(), after=stale)
        self.player.restart_current()          # bumps the generation
        calls = self._recorder()

        stale(None)                            # the abandoned source reports it ended
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(calls, [], "an abandoned source advanced the queue")

    async def test_restart_is_refused_when_nothing_is_playing(self):
        self.player.current = _track("Nothing playing")
        self.assertFalse(self.player.restart_current())
        self.assertEqual(self.vc.stop_calls, 0)

    async def test_restart_is_refused_without_a_stream_url(self):
        self.player.current = _track("Spotify pending", stream_url="")
        self.vc._playing = True
        self.assertFalse(self.player.restart_current())


class ATruncatedStreamIsToldApartFromAFinishedOne(_PlayerCase):
    """The "short music" bug, pinned from both directions."""

    async def _run_source(self, played_seconds: float, duration: int = 200, live: bool = False):
        faults = []

        async def collect(fault):
            faults.append(fault)

        self.player.on_fault = collect
        self.player.current = _track("A Song", duration=duration, live=live)
        self.player._expected = float(duration)
        # Pretend the source started `played_seconds` ago.
        self.player._started_at = player_module.time.monotonic() - played_seconds
        self.player._generation = 5
        self.player.play_next = mock.AsyncMock()
        self.player._after_for(5)(None)
        for _ in range(4):
            await asyncio.sleep(0)
        return faults

    async def test_a_stream_that_ends_early_is_reported_as_a_fault(self):
        faults = await self._run_source(played_seconds=12, duration=200)

        self.assertEqual([f.kind for f in faults], ["truncated"])
        self.assertIn("cut-off", faults[0].detail)
        self.assertEqual(faults[0].guild_id, GUILD)

    async def test_a_track_that_reaches_its_end_is_not_a_fault(self):
        faults = await self._run_source(played_seconds=199, duration=200)
        self.assertEqual(faults, [])

    async def test_a_genuinely_short_track_is_not_judged(self):
        # Under the floor there is nothing to be wrong: an intro, a stinger, a
        # live join. Judging those would make the card cry wolf.
        faults = await self._run_source(played_seconds=10, duration=20)
        self.assertEqual(faults, [])

    async def test_a_live_stream_is_never_truncated(self):
        faults = await self._run_source(played_seconds=30, duration=600, live=True)
        self.assertEqual(faults, [])

    async def test_an_engine_error_is_reported_with_its_own_kind(self):
        faults = []

        async def collect(fault):
            faults.append(fault)

        self.player.on_fault = collect
        self.player.current = _track("A Song")
        self.player._expected = 180.0
        self.player._generation = 3
        self.player.play_next = mock.AsyncMock()
        self.player._after_for(3)(RuntimeError("pipe broken"))
        for _ in range(4):
            await asyncio.sleep(0)

        self.assertEqual([f.kind for f in faults], ["source_error"])
        self.assertIn("pipe broken", faults[0].detail)


class SourceSelection(unittest.TestCase):
    """Three paths, and the reason each one is taken."""

    def setUp(self):
        self.bot = SimpleNamespace(loop=None)
        self.player = player_module.GuildPlayer(self.bot, GUILD)
        self.player.voice_client = _FakeVoiceClient()

    def _current(self, *, opus=True, abr=131.0):
        track = _track("Song")
        track.is_opus = opus
        track.abr = abr
        self.player.current = track

    def test_opus_that_fits_the_channel_is_passed_through(self):
        self._current(opus=True, abr=131.0)
        self.player.volume = 1.0
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            _source, engine = self.player._build_source("http://example.invalid/a")

        self.assertEqual(opus_cls.call_args.kwargs.get("codec"), "copy")
        self.assertNotIn("options", opus_cls.call_args.kwargs)
        self.assertIn("pass-through", engine)

    def test_the_default_volume_is_one_so_the_pass_through_path_is_reachable(self):
        # The archived cog shipped 0.7, which silently disqualified *every* track
        # from the pass-through path — a re-encode by default, which is the
        # quality complaint written down as a number.
        self.assertEqual(player_module.GuildPlayer(SimpleNamespace(loop=None), 1).volume, 1.0)

    def test_a_non_unity_volume_re_encodes(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 0.5
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))
        self.assertIn("volume=0.5", opus_cls.call_args.kwargs["options"])

    def test_with_libopus_the_volume_is_live(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 0.25
        with mock.patch.object(player_module.voice_caps, "probe") as probe, \
             mock.patch.object(player_module.discord, "FFmpegPCMAudio"), \
             mock.patch.object(player_module.discord, "PCMVolumeTransformer") as vol_cls:
            probe.return_value = SimpleNamespace(ffmpeg="/usr/bin/ffmpeg", opus=True)
            self.player._build_source("http://example.invalid/a")

        self.assertEqual(vol_cls.call_args.kwargs["volume"], 0.25)

    def test_a_filter_beats_the_pass_through_path(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 1.0
        self.player.filter_name = "nightcore"
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))
        self.assertIn(
            player_module.FILTER_BY_KEY["nightcore"].args, opus_cls.call_args.kwargs["options"]
        )

    def test_the_ffmpeg_command_line_does_not_carry_the_truncating_flags(self):
        # ``-err_detect ignore_err -probesize 32768 -analyzeduration 0`` tells
        # ffmpeg to ignore the errors that would explain a truncation and to start
        # playing before it knows what the container is. They were in the archived
        # command line and they are the prime suspects for the short-song report.
        for flag in ("-err_detect", "ignore_err", "-analyzeduration", "-probesize"):
            self.assertNotIn(flag, player_module.BEFORE_OPTIONS)
        self.assertIn("-reconnect", player_module.BEFORE_OPTIONS)


class SeekRestartsInPlace(_PlayerCase):
    async def test_a_seek_sets_the_offset_and_re_renders(self):
        self.player.current = _track("Song", duration=200)
        self.player._expected = 200.0
        self.vc._playing = True
        with mock.patch.object(self.player, "_build_source", return_value=(object(), "engine")):
            applied, position = self.player.seek(90)

        self.assertTrue(applied)
        self.assertEqual(position, 90.0)
        self.assertEqual(self.player.seek_offset, 90.0)

    async def test_a_seek_past_the_end_is_refused(self):
        self.player.current = _track("Song", duration=100)
        self.vc._playing = True
        with mock.patch.object(self.player, "_build_source", return_value=(object(), "engine")):
            applied, position = self.player.seek(500)

        self.assertFalse(applied)
        self.assertEqual(position, 0.0)
        self.assertEqual(self.player.seek_offset, 0.0)


class LoopDoesNotEatTheQueue(_PlayerCase):
    async def test_loop_track_puts_the_current_track_back(self):
        first = _track("Repeat me")
        self.player.current = first
        self.player.queue = []
        self.player.loop = "track"
        self.player._expected = 5.0
        self.vc._playing = True

        started = []
        with mock.patch.object(self.player, "_build_source", return_value=(object(), "engine")):
            self.vc.play = lambda source, after=None: started.append(source)
            await self.player.play_next()

        self.assertIs(self.player.current, first, "the looped track was lost")

    async def test_loop_off_moves_on(self):
        self.player.current = _track("First")
        self.player.queue = [_track("Second")]
        self.player.loop = "off"
        with mock.patch.object(self.player, "_build_source", return_value=(object(), "engine")):
            self.vc.play = lambda source, after=None: None
            await self.player.play_next()

        self.assertEqual(self.player.current.title, "Second")
        self.assertEqual(self.player.queue, [])


class FormatDuration(unittest.TestCase):
    def test_a_live_stream_reads_as_live(self):
        self.assertEqual(player_module.format_duration(0), "live")
        self.assertEqual(player_module.format_duration(-5), "live")

    def test_minutes_and_seconds(self):
        self.assertEqual(player_module.format_duration(65), "1:05")

    def test_hours(self):
        self.assertEqual(player_module.format_duration(3725), "1:02:05")


class TheCommandSurfaceFitsDiscord(unittest.TestCase):
    """Discord allows 100 top-level commands; this bot publishes most of them."""

    def test_the_tree_publishes_the_two_roots_the_registry_declares(self):
        from utils import features

        self.assertIn("play", features.MODULE_COMMAND_MAP)
        self.assertIn("music", features.MODULE_COMMAND_MAP)
        self.assertEqual(
            features.MODULE_COMMAND_MAP["play"], ("music",),
            "/play must be gated by the music feature and nothing else",
        )

    def test_the_count_is_under_the_ceiling(self):
        from utils import features

        self.assertLessEqual(
            features.command_count(), 100,
            "Discord refuses a new command at 100 per scope — this is the ceiling "
            "the archived cog's twelve roots would have blown through",
        )

    def test_the_cog_declares_one_group_and_one_play_command(self):
        import cogs.music as music_cog

        names = {command.name for command in music_cog.Music.__cog_app_commands__}
        # Exactly two, and that is the decision this pins: the archived cog had
        # twelve top-level commands, which would not fit Discord's ceiling.
        self.assertEqual(names, {"play", "music"})

    def test_the_group_holds_every_other_command(self):
        import cogs.music as music_cog

        group = next(
            command for command in music_cog.Music.__cog_app_commands__ if command.name == "music"
        )
        names = {child.name for child in group.commands}
        for expected in ("radio", "skip", "pause", "resume", "stop", "queue", "nowplaying"):
            self.assertIn(expected, names)
        self.assertLessEqual(len(names), 24, "a group the picker cannot render is not a group")

    def test_every_subcommand_is_declared_for_a_permission_audience(self):
        from utils import permissions

        import cogs.music as music_cog

        group = next(
            command for command in music_cog.Music.__cog_app_commands__ if command.name == "music"
        )
        for child in group.commands:
            self.assertIn(
                f"music {child.name}", permissions.COMMAND_ACCESS,
                f"/music {child.name} has no declared audience",
            )


class MusicCardsDoNotPing(unittest.TestCase):
    def test_no_user_mentions_in_music_output(self):
        source = pathlib.Path("cogs/music.py").read_text(encoding="utf-8")

        # A channel mention never notifies; a user mention does.
        self.assertNotIn("interaction.user.mention", source)
        self.assertNotIn("member.mention", source)
        self.assertIn("interaction.user.display_name", source)


if __name__ == "__main__":
    unittest.main()
