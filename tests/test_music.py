"""Radio, tested without a Discord connection: the player's two silent failures.

Both of these were *shipped* bugs, and neither had a symptom that pointed at
itself.

**A deliberate stop is not a finished song.** Applying a filter or changing the
volume replaces the audio source, and the old source's after-callback fires on
the way out — so the queue advanced too, and changing a filter ate a queued
track. A generation counter fixes it: every source's callback is tagged with the
generation it belongs to, and a callback only advances the queue while its
generation is current.

**A stream that stops is not a stream that ended.** ffmpeg reporting an exit
arrives as ``err=None``, exactly like a track finishing, and a *clean* EOF looks
even more like one — which is what "the music is short" was. A station has no
end, so a live source that reports itself finished was cut; a source that
declares a length is judged against it.

ffmpeg is never invoked here: the source classes are mocked and ``play_next``'s
network half is replaced with a recorder. Nothing resolves anything any more, so
nothing is stubbed for a resolver — the archived suite
(``deleted_features/tests/test_music_matching.py``) has that half.
"""
import asyncio
import os
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from utils import player as player_module
from utils.media_source import Track

GUILD = 321


def _track(title: str, **kwargs) -> Track:
    """A track with sensible defaults; ``kwargs`` wins, including ``duration``.

    Live by default, because that is what a station is: no end, and therefore
    judged differently from something that declares a length.
    """
    fields = {
        "title": title,
        "provider": "radio",
        "stream_url": "http://example.invalid/stream",
        "duration": 0,
        "requester": "Raj",
        "live": True,
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


class AStationWithNoAddressIsNamedRatherThanSkipped(_PlayerCase):
    """The one way a radio track can still be unplayable, and what it says.

    There is nothing left to resolve an address *with*: the extractor that turned
    words into one is archived, and a station arrives with its own. So a track in
    the queue without one is a defect in whatever queued it rather than a refused
    provider — and either way, skipping it in silence is the thing this player
    exists not to do.
    """

    def _faults(self) -> list:
        """Collect the faults the player reports, and wait for them.

        ``_report`` hands the coroutine to ``asyncio.run_coroutine_threadsafe``
        rather than awaiting it — deliberately, so a slow card cannot stall the
        audio thread. That makes the *delivery* asynchronous, so a test that reads
        the list straight after ``play_next`` reads it before it was filled; hence
        :meth:`_settle` rather than a bare assertion.
        """
        self.faults = []

        async def record(fault):
            self.faults.append(fault)

        self.player.on_fault = record
        return self.faults

    async def _settle(self, count: int = 1) -> None:
        """Give the loop room to deliver ``count`` scheduled notifications."""
        for _ in range(100):
            if len(self.faults) >= count:
                return
            await asyncio.sleep(0.005)
        self.fail(f"only {len(self.faults)} fault(s) arrived, expected {count}")

    async def test_a_track_with_no_address_is_reported_as_unplayable(self):
        self.player.queue = [_track("Nameless", stream_url="")]
        faults = self._faults()

        await self.player.play_next()
        await self._settle()

        self.assertIsNone(self.player.current)
        self.assertEqual([f.kind for f in faults], ["unplayable"])
        self.assertIn("no address", faults[0].detail)
        self.assertEqual(self.vc.played, [], "nothing should have reached the voice client")

    async def test_a_station_is_not_sent_looking_for_anything(self):
        """The common path must not pay for the rare one, and cannot: nothing resolves."""
        self.player.queue = [_track("Fine")]
        await self.player.play_next()
        self.assertIsNotNone(self.player.current)
        self.assertEqual(len(self.vc.played), 1)


class AStreamThatStopsIsToldApartFromOneThatEnds(_PlayerCase):
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

    async def test_a_station_that_reports_itself_finished_was_cut(self):
        """A live stream has no end, so *any* clean ending is a dropped connection.

        This is the radio shape of the bug that produced the whole guard: the
        station closes the socket, ffmpeg exits 0, and without this the room hears
        silence and the queue moves on as though the song had finished.
        """
        faults = await self._run_source(played_seconds=90, duration=0, live=True)

        self.assertEqual([f.kind for f in faults], ["truncated"])
        self.assertIn("went quiet", faults[0].detail)
        self.assertIn("dropped connection", faults[0].detail)

    async def test_a_source_that_declares_no_length_and_is_not_live_is_not_judged(self):
        # Nothing to judge it against, and guessing from elapsed time alone would
        # make the card cry wolf on every short file.
        faults = await self._run_source(played_seconds=10, duration=0, live=False)
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

    def test_a_song_keeps_its_own_bitrate_on_an_ordinary_channel(self):
        """The regression behind the quality report, stated as the number it is.

        A normal Discord voice channel is **64 kbps**. The old gate refused to pass
        through anything louder than that, so a 131 kbps Opus song was decoded and
        re-encoded down to 64 on the way out — every song, on every unboosted
        server — which is what an echo and a thin, hollow mix sound like.
        """
        self._current(opus=True, abr=131.0)
        self.player.volume = 1.0
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            _source, engine = self.player._build_source("http://example.invalid/a", 64)

        self.assertEqual(opus_cls.call_args.kwargs.get("codec"), "copy")
        self.assertIn("131 kbps", engine)

    def test_encoding_never_goes_below_the_source_or_below_the_channel(self):
        # 160 kbps of MP3 asked for on a 64 kbps channel: encoding at 64 would
        # discard more than half of a recording that arrived intact.
        self.assertEqual(player_module.encode_bitrate(64, SimpleNamespace(abr=160.0)), 160)
        # A boosted channel wants 384, which is past the point music gains anything.
        self.assertEqual(player_module.encode_bitrate(384, SimpleNamespace(abr=96.0)), 256)
        # Nothing known about the source: the channel's own number is the floor.
        self.assertEqual(player_module.encode_bitrate(96, SimpleNamespace(abr=0)), 96)
        self.assertEqual(player_module.encode_bitrate(0, SimpleNamespace(abr=0)), 64)

    def test_a_non_opus_source_is_never_passed_through(self):
        self._current(opus=False, abr=160.0)
        self.player.volume = 1.0
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a", 64)

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))

    def test_the_encoder_is_told_this_is_music(self):
        """Measured against a musical signal at 96 kbps: discord.py's own
        ``-fec true -packet_loss 15`` cost 4.3 dB and ``-application voip`` a
        further 6.9 dB, because the redundancy is carried by libopus's speech
        path. Neither is the right trade for a music player."""
        self._current(opus=False, abr=160.0)
        self.player.volume = 1.0
        with mock.patch.object(player_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            _source, engine = self.player._build_source("http://example.invalid/a", 64)

        options = opus_cls.call_args.kwargs["options"]
        self.assertIn("-application audio", options)
        self.assertIn("-fec false", options)
        self.assertIn("-packet_loss 0", options)
        self.assertIn("-compression_level 10", options)
        self.assertEqual(opus_cls.call_args.kwargs["bitrate"], 160)
        # And the card says which of the three paths ran, so a bad-sounding song
        # is a reading rather than a guess.
        self.assertIn("160 kbps", engine)

    def test_a_filter_and_a_volume_change_share_one_filtergraph(self):
        """Two ``-af`` flags are not a chain: ffmpeg replaces the first with the
        second, which is how a filter chosen here used to vanish in silence."""
        self._current(opus=True, abr=96.0)
        self.player.volume = 0.5
        self.player.filter_name = "nightcore"
        with mock.patch.object(player_module.voice_caps, "probe") as probe, \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            probe.return_value = SimpleNamespace(ffmpeg="/usr/bin/ffmpeg", opus=False)
            self.player._build_source("http://example.invalid/a", 96)

        options = opus_cls.call_args.kwargs["options"]
        self.assertEqual(options.count("-af"), 1)
        self.assertIn(player_module.FILTER_BY_KEY["nightcore"].args, options)
        self.assertIn("volume=0.5", options)

    def test_a_station_is_never_sent_through_a_proxy(self):
        """A station URL is public and needs no proxy, so ffmpeg gets none.

        The proxy existed because a *provider* signed its stream URL for the
        address that asked for it. Radio is the thing that never needed one, and
        it is now the only thing there is — so nothing here may acquire a way to
        fail that only a deployment with an outbound proxy can reach.
        """
        self._current(opus=True, abr=131.0)
        self.player.volume = 1.0
        with mock.patch.dict(os.environ, {"YTDL_PROXY": "http://proxy.test:8080"}, clear=False), \
             mock.patch.object(player_module.discord, "FFmpegOpusAudio") as opus_cls:
            _source, _engine = self.player._build_source("http://example.invalid/a", 64)

        before = opus_cls.call_args.kwargs["before_options"]
        self.assertNotIn("-http_proxy", before)
        # The reconnect flags are the ones a station *does* need: an interrupted
        # icecast connection should be retried rather than treated as over.
        self.assertIn("-reconnect", before)


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

    def test_the_tree_publishes_the_one_root_the_registry_declares(self):
        from utils import features

        self.assertIn("music", features.MODULE_COMMAND_MAP)
        self.assertEqual(
            features.MODULE_COMMAND_MAP["music"], ("music",),
            "/music is gated by the music feature and nothing else",
        )
        self.assertNotIn(
            "play", features.MODULE_COMMAND_MAP,
            "/play was removed with the extractor, so nothing may be gated by it",
        )

    def test_the_count_is_under_the_ceiling(self):
        from utils import features

        self.assertLessEqual(
            features.command_count(), 100,
            "Discord refuses a new command at 100 per scope — this is the ceiling "
            "the archived cog's twelve roots would have blown through",
        )

    def test_the_cog_declares_one_root_and_no_play_command(self):
        import cogs.music as music_cog

        names = {command.name for command in music_cog.Music.__cog_app_commands__}
        # One root, and that is the decision this pins: the cog had two, and the
        # root it gave up is the one whose whole surface is archived.
        self.assertEqual(names, {"music"})

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


class TheFaultCardSaysWhatActuallyHappened(unittest.TestCase):
    """*"That stream was cut off"* over a provider that never gave a stream.

    The card had one headline for every kind of failure. In the screenshot that
    produced this, nothing had been cut off at all: the provider refused the host
    (``DownloadError``), which is a different problem with a different fix — and the
    card was sending whoever read it to look at the wrong thing. `Fault.headline`
    had named the kinds all along; the card is what ignored it.
    """

    #: Every kind the player can raise. Named here so a new `Fault` kind is caught
    #: by this test rather than by a card that quietly falls back to the generic one
    #: — which is how the original defect existed. The two kinds that are gone with
    #: the extractor (``blocked`` and ``rescued``) are asserted *absent* below, so a
    #: card that outlives its mechanism fails here rather than shipping.
    KINDS = ("truncated", "unplayable", "source_error", "opus_missing")

    def _cards(self):
        import cogs.music as music_cog

        return music_cog.Music.FAULT_CARDS

    def test_every_kind_the_player_can_raise_has_its_own_card(self):
        import utils.player as player_module

        cards = self._cards()
        self.assertEqual(
            sorted(cards), sorted(self.KINDS),
            "the card table and the player's own kinds have drifted apart",
        )
        for kind in self.KINDS:
            headline, _, advice = cards[kind]
            self.assertTrue(headline.strip(), f"{kind} has no headline")
            self.assertTrue(advice.strip(), f"{kind} says nothing about what to do")
            # The player names each kind too, and the card must not contradict it.
            self.assertTrue(player_module.Fault(kind=kind, detail="").headline)

    def test_every_kind_reads_differently(self):
        """One headline per kind is the whole point: a table with repeats is the bug."""
        cards = self._cards()
        headlines = [cards[kind][0] for kind in self.KINDS]
        self.assertEqual(len(set(headlines)), len(headlines))

    def test_a_station_that_will_not_open_does_not_claim_a_stream_was_cut_off(self):
        cards = self._cards()

        cut_off, _, cut_off_advice = cards["truncated"]
        refused, _, refused_advice = cards["unplayable"]

        self.assertIn("cut off", cut_off.lower())
        self.assertNotIn("cut off", refused.lower(), "the wrong headline again")
        self.assertNotEqual(cut_off_advice, refused_advice)
        self.assertIn("host", refused_advice.lower())

    def test_the_extractor_s_own_kinds_are_gone(self):
        """Two cards outlived their mechanism for a whole pass once already.

        ``blocked`` was "the provider refused this host" and ``rescued`` was a
        substituted upload — neither can be raised now that nothing resolves
        anything, and a card for a fault that cannot happen is a sentence somebody
        reads while looking for a provider this bot does not have.
        """
        import utils.player as player_module

        for kind in ("blocked", "rescued"):
            self.assertNotIn(kind, self._cards())
            self.assertEqual(
                player_module.Fault(kind=kind, detail="").headline, kind,
                "a retired kind must not keep a hand-written headline",
            )


class NothingLiveStillSellsTheExtractor(unittest.TestCase):
    """The removal is only finished when nothing live still *offers* the extractor.

    This project's recurring defect is prose that outlives its mechanism — a help
    panel naming a command that does not exist, a landing page describing a search
    that was deleted, a note on a constant claiming an extension the code does not
    have — so the surfaces are checked by name rather than left to a reviewer.

    **The rule has a second half, and it is the one that makes it implementable:
    naming the package where its removal is explained is not the defect.** The
    archive README has to keep naming `yt-dlp` or a restore becomes a guess, the
    changelog has to or the removal has no record, and three docstrings say where
    the resolver went. What may not happen is a *capability*: a command, a help
    line, a registry entry, a public page or an installed requirement.
    """

    #: Files whose text a **member or a server's administrator** reads as text.
    USER_FACING = (
        "cogs/help.py",
        "utils/features.py",
        "utils/permissions.py",
        "templates/music.html",
    )

    def test_no_user_facing_page_offers_it(self):
        for path in self.USER_FACING:
            with self.subTest(path=path):
                source = pathlib.Path(path).read_text(encoding="utf-8").lower()
                for word in ("yt-dlp", "yt_dlp", "/play", "songinfo"):
                    self.assertNotIn(word, source, f"{path} still offers {word}")

    def test_nothing_live_imports_it(self):
        for path in (
            "cogs/music.py",
            "utils/media_source.py",
            "utils/media_health.py",
            "utils/player.py",
            "keep_alive.py",
            "bot.py",
        ):
            with self.subTest(path=path):
                source = pathlib.Path(path).read_text(encoding="utf-8")
                self.assertNotIn("import yt_dlp", source)
                self.assertNotIn("from yt_dlp", source)

    def test_the_doctor_card_reads_the_stream_instead(self):
        """The one card an operator reads about this, checked where it is written.

        It is a *string* in the command body, so nothing else in the suite would
        notice a line about a package that is no longer installed.
        """
        source = pathlib.Path("cogs/music.py").read_text(encoding="utf-8")
        self.assertIn("**Streams**", source)
        for gone in ("**Extractor**", "**yt-dlp**", "**Client**", "**Sources**"):
            self.assertNotIn(gone, source, f"the doctor card still prints {gone}")

    def test_requirements_no_longer_installs_it(self):
        """A comment may explain the removal; a line may not install the package.

        And `imageio-ffmpeg` must stay: ffmpeg decodes a station's MP3/AAC and
        encodes Opus for Discord, so it is the *player* rather than the extractor,
        and a host without it has a bot that joins a channel and says nothing.
        """
        lines = [line.strip() for line in pathlib.Path("requirements.txt").read_text(encoding="utf-8").splitlines()]
        requirements = [line for line in lines if line and not line.startswith("#")]
        self.assertFalse(
            [line for line in requirements if line.lower().startswith("yt-dlp")],
            "yt-dlp is still installed as a requirement",
        )
        self.assertTrue(
            [line for line in requirements if line.lower().startswith("imageio-ffmpeg")],
            "ffmpeg was removed with the extractor, and it is what plays a station",
        )

    def test_the_archive_still_says_how_to_restore_it(self):
        """The other half: what may not be forgotten is how to put it back."""
        readme = pathlib.Path("deleted_features/README.md").read_text(encoding="utf-8")
        self.assertIn("yt-dlp", readme)
        for name in (
            "deleted_features/utils/media_source.py",
            "deleted_features/tests/test_music_matching.py",
            "deleted_features/tests/test_music_playback.py",
        ):
            self.assertTrue(pathlib.Path(name).exists(), f"the archive is missing {name}")
            self.assertIn(pathlib.Path(name).name, readme, f"the README does not mention {name}")


class MusicCardsDoNotPing(unittest.TestCase):
    def test_no_user_mentions_in_music_output(self):
        source = pathlib.Path("cogs/music.py").read_text(encoding="utf-8")

        # A channel mention never notifies; a user mention does.
        self.assertNotIn("interaction.user.mention", source)
        self.assertNotIn("member.mention", source)
        self.assertIn("interaction.user.display_name", source)


if __name__ == "__main__":
    unittest.main()
