"""Music: picking the *right* recording, and streaming it at full quality.

Two production complaints drove these fixes: a Spotify link showed one song but
played a different one, and the audio sounded worse than it should.

The wrong-song half was a matching bug. ``play`` and ``_resolve_stream_url`` each
carried their own copy of a scorer whose duration term was an uncapped seconds
delta, so any re-upload that happened to match the Spotify album length outranked
the artist's own (shorter) radio edit — "Get Lucky" resolved to a re-upload called
EXPO STORY instead of Daft Punk's official audio. On top of that, re-resolving a
stream derived its search from ``track.title``, which is "Title - Artist", while
the original search was "Artist - Title": a different query that could land on a
different video than the card showed.

The quality half was one line: a hardcoded
``extractor_args: {"youtube": {"player_client": ["android", "web"]}}``. That client
set exposes *only* YouTube's muxed 360p MP4 (format 18 — low-bitrate AAC in a video
container), so ``bestaudio/best`` fell back to a video stream and every song was
demuxed and re-encoded from it. Dropping the override negotiates the audio-only
Opus format (251, ~130 kbps).

None of these tests touch the network: results are hand-built the way yt-dlp's flat
extraction presents them.
"""
import pathlib
import unittest
from unittest import mock

if not (pathlib.Path(__file__).resolve().parent.parent / "cogs" / "music.py").exists():
    # Paired with the guard in test_music.py: the cog this covers was archived,
    # so skip rather than failing to import.
    raise unittest.SkipTest("cogs/music.py is not in this build")

from cogs import music as M
from tests.test_music import _FakeVoiceClient, _MusicCase, _track


def _entry(title, channel, duration, views=0, verified=False):
    """A yt-dlp flat search result, shaped like the real dict."""
    return {
        "title": title,
        "channel": channel,
        "uploader": channel,
        "duration": duration,
        "view_count": views,
        "channel_is_verified": verified,
        "url": f"http://example.invalid/{title}",
        "webpage_url": f"http://example.invalid/{title}",
    }


class NoMoreDowngradedAudio(unittest.TestCase):
    """The extractor must not force the low-quality client set."""

    def test_the_android_web_override_is_gone(self):
        for options in (M.YTDL_FLAT_OPTIONS, M.YTDL_STREAM_OPTIONS):
            self.assertNotIn(
                "extractor_args", options,
                "the hardcoded player_client only exposed a muxed 360p MP4",
            )

    def test_stream_format_prefers_audio_only_webm(self):
        self.assertTrue(M.YTDL_STREAM_OPTIONS["format"].startswith("bestaudio[ext=webm]"))

    def test_the_legacy_client_is_only_used_as_a_fallback(self):
        calls = []
        good = {"title": "ok", "url": "http://x"}

        def fake_youtube_dl(options):
            calls.append(options)

            class _Handle:
                def __enter__(self_inner):
                    if len(calls) == 1:
                        raise RuntimeError("Requested format is not available")
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def extract_info(self_inner, target, download=False):
                    return good

            return _Handle()

        with mock.patch.object(M.yt_dlp, "YoutubeDL", side_effect=fake_youtube_dl):
            result = M.ytdl_extract(dict(M.YTDL_STREAM_OPTIONS), "ytsearch1:song")

        self.assertIs(result, good)
        self.assertNotIn("extractor_args", calls[0])
        self.assertEqual(calls[1]["extractor_args"]["youtube"]["player_client"], ["android", "web"])

    def test_a_failure_of_the_fallback_propagates(self):
        with mock.patch.object(M.yt_dlp, "YoutubeDL", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                M.ytdl_extract(dict(M.YTDL_STREAM_OPTIONS), "ytsearch1:song")


class StreamCopy(unittest.TestCase):
    """Fresh Opus that fits the channel is passed through, not re-encoded."""

    def setUp(self):
        self.player = M.GuildMusicPlayer(mock.Mock(), 42)
        self.player.voice_client = _FakeVoiceClient()
        self.player.bitrate_kbps = 128

    def _current(self, opus=True, abr=131.0):
        track = _track("Song")
        track.source_is_opus = opus
        track.source_abr = abr
        self.player.current = track

    def test_opus_within_the_channel_bitrate_is_copied(self):
        self._current(opus=True, abr=131.0)
        self.player.volume = 1.0
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertEqual(opus_cls.call_args.kwargs.get("codec"), "copy")
        self.assertNotIn("options", opus_cls.call_args.kwargs)
        self.assertIn("stream-copy", self.player._engine)

    def test_copy_is_preferred_even_where_libopus_loads(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 1.0
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=True), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls, \
             mock.patch.object(M.discord, "FFmpegPCMAudio") as pcm_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertEqual(opus_cls.call_args.kwargs.get("codec"), "copy")
        pcm_cls.assert_not_called()

    def test_a_non_unity_volume_re_encodes(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 0.5
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))
        self.assertIn("volume=0.5", opus_cls.call_args.kwargs["options"])

    def test_a_source_louder_than_the_channel_re_encodes(self):
        self._current(opus=True, abr=300.0)      # e.g. a 64 kbps channel
        self.player.bitrate_kbps = 64
        self.player.volume = 1.0
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))

    def test_a_non_opus_source_is_never_copied(self):
        self._current(opus=False, abr=128.0)
        self.player.volume = 1.0
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))

    def test_a_filter_disables_stream_copy(self):
        self._current(opus=True, abr=96.0)
        self.player.volume = 1.0
        self.player.current_filter = "nightcore"
        with mock.patch.object(M.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(M.discord, "FFmpegOpusAudio") as opus_cls:
            self.player._build_source("http://example.invalid/a")

        self.assertIsNone(opus_cls.call_args.kwargs.get("codec"))
        self.assertIn(M.AUDIO_FILTERS["nightcore"]["ffmpeg"], opus_cls.call_args.kwargs["options"])


class MatchingTheRightRecording(unittest.TestCase):
    def test_the_artists_own_upload_beats_a_same_length_reupload(self):
        # The real production case: Spotify's album version runs 369s, the artist's
        # YouTube upload is the 249s radio edit, and a re-upload named EXPO STORY
        # matched 369s exactly. The old duration-dominated scorer returned the
        # re-upload; the artist's own verified channel must win.
        reupload = _entry("Daft Punk - Get Lucky (Feat. Pharrell Williams)", "EXPO STORY", 369, views=40_000_000)
        official = _entry(
            "Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers",
            "Daft Punk", 249, views=900_000_000, verified=True,
        )

        picked = M._best_match([reupload, official], "Daft Punk - Get Lucky", 369, "Daft Punk")

        self.assertIs(picked, official)

    def test_a_slowed_reupload_never_wins(self):
        slowed = _entry("One Dance - Drake | Slowed + Reverbed | Attractive Playlist", "Invincible Vibes", 194, views=11_000_000)
        lyrics = _entry("Drake - One Dance (Lyrics) ft. Wizkid & Kyla", "Billion Stars", 175, views=420_000_000)

        picked = M._best_match([slowed, lyrics], "Drake, Wizkid, Kyla - One Dance", 173, "Drake")

        self.assertIs(picked, lyrics)

    def test_the_junk_penalty_lowers_the_score_rather_than_raising_it(self):
        # Lower is better. This assertion exists because the first cut subtracted the
        # penalty, which *rewarded* remixes — the live run returned one.
        plain = _entry("Song", "Someone", 200, views=1000)
        remix = _entry("Song (Bonus Remix)", "Someone", 200, views=1000)

        self.assertLess(
            M._score_candidate(plain, "Artist - Song", 200),
            M._score_candidate(remix, "Artist - Song", 200),
        )

    def test_word_boundaries_stop_false_matches(self):
        self.assertEqual(M._junk_penalty("discover the world", "x"), 0.0)
        self.assertEqual(M._junk_penalty("song (cover)", "x"), M._JUNK_PENALTY)

    def test_a_flavour_the_user_asked_for_is_not_penalised(self):
        self.assertEqual(M._junk_penalty("song (extended remix)", "artist - song remix"), 0.0)

    def test_live_recordings_are_penalised_only_when_not_requested(self):
        live = _entry("Artist - Song (Live at Wembley)", "Artist", 260, views=5_000_000, verified=True)
        studio = _entry("Artist - Song (Official Audio)", "Artist", 200, views=90_000_000, verified=True)

        picked = M._best_match([live, studio], "Artist - Song", 200, "Artist")
        self.assertIs(picked, studio, "a live take outranked the studio recording")

        picked_live = M._best_match([live, studio], "Artist - Song live", 260, None)
        self.assertIs(picked_live, live)

    def test_a_fan_channel_mentioning_the_name_is_not_the_artist(self):
        fan = _entry("Song", "Drake Fan Uploads", 200)
        own = _entry("Song", "Drake", 200, verified=True)

        self.assertEqual(M._artist_strength(fan, "Drake"), 1)
        self.assertEqual(M._artist_strength(own, "Drake"), 2)

    def test_vevo_and_topic_feeds_count_as_the_artist(self):
        vevo = _entry("Song", "AdeleVEVO", 200)
        topic = _entry("Song", "Adele - Topic", 200)

        self.assertEqual(M._artist_strength(vevo, "Adele"), 2)
        self.assertEqual(M._artist_strength(topic, "Adele"), 2)

    def test_collaborators_are_reduced_to_the_primary_artist(self):
        self.assertEqual(M._clean_artist("Daft Punk, Pharrell Williams, Nile Rodgers"), "Daft Punk")
        self.assertEqual(M._clean_artist("Tones and I"), "Tones and I")
        self.assertEqual(M._clean_artist("Drake feat. Wizkid"), "Drake")
        self.assertIsNone(M._clean_artist("Unknown Artist"))
        self.assertIsNone(M._clean_artist(""))

    def test_an_artist_can_be_read_out_of_a_plain_query(self):
        self.assertEqual(M._artist_from_query("Rick Astley - Never Gonna Give You Up"), "Rick Astley")
        self.assertIsNone(M._artist_from_query("never gonna give you up"))
        self.assertIsNone(M._artist_from_query("https://youtu.be/abc"))

    def test_bot_checks_are_recognised_without_false_positives(self):
        self.assertTrue(M._is_bot_block(Exception("HTTP Error 429: Too Many Requests")))
        self.assertTrue(M._is_bot_block(Exception("Sign in to confirm you're not a bot")))
        self.assertFalse(M._is_bot_block(Exception("unable to bootstrap the extractor")))

    def test_without_an_artist_the_whole_result_set_is_still_considered(self):
        entries = [
            _entry("Obscure Song (Official Audio)", "Tiny Channel", 180, views=500),
            _entry("Obscure Song", "Another Channel", 181, views=900_000),
        ]

        picked = M._best_match(entries, "Obscure Song", 180, None)

        self.assertIn(picked, entries)


class TrackRemembersHowItWasFound(unittest.TestCase):
    def test_the_track_carries_its_search_query_and_source_metadata(self):
        track = M.Track(
            title="Song - Artist",
            source_url="http://example.invalid/s",
            search_query="Artist - Song",
            expected_artist="Artist",
        )

        self.assertEqual(track.search_query, "Artist - Song")
        self.assertEqual(track.expected_artist, "Artist")
        self.assertEqual(track.resolved_at, 0.0, "an unresolved track must not look fresh")

    def test_stream_metadata_is_recorded_for_the_player(self):
        track = M.Track(title="t", source_url="u")

        M._apply_stream_meta(track, {"url": "u", "ext": "webm", "acodec": "opus", "abr": 131.596})

        self.assertTrue(track.source_is_opus)
        self.assertAlmostEqual(track.source_abr, 131.596, places=3)
        self.assertGreater(track.resolved_at, 0.0)

    def test_an_aac_stream_is_not_marked_opus(self):
        track = M.Track(title="t", source_url="u")

        M._apply_stream_meta(track, {"url": "u", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 129.5})

        self.assertFalse(track.source_is_opus)
        self.assertAlmostEqual(track.source_abr, 129.5, places=3)


class ReResolutionUsesTheOriginalSearch(_MusicCase):
    """A refresh must not silently substitute a different video."""

    async def _run(self, track):
        player, vc = self._player()
        player._build_source = lambda url: object()
        player.queue = [track]
        seen = []

        async def fake_resolve(t):
            seen.append(t)
            return "http://example.invalid/resolved"

        player._resolve_stream_url = fake_resolve
        await player.play_next()
        return player, seen

    async def test_a_fresh_url_is_not_searched_again(self):
        track = _track("Artist - Song")
        track.source_url = "https://rr1.googlevideo.com/videoplayback?x=1"
        track.search_query = "Artist - Song"
        M._apply_stream_meta(track, {"url": track.source_url, "ext": "webm", "acodec": "opus", "abr": 130})

        player, seen = await self._run(track)

        self.assertEqual(seen, [], "a freshly-resolved URL was searched a second time")
        self.assertEqual(player.current.source_url, "https://rr1.googlevideo.com/videoplayback?x=1")

    async def test_a_stale_url_is_refreshed(self):
        track = _track("Artist - Song")
        track.source_url = "https://rr1.googlevideo.com/videoplayback?x=1"
        track.resolved_at = 0.0        # resolved long ago
        M._apply_stream_meta(track, {"url": "x", "ext": "webm", "acodec": "opus", "abr": 130})
        track.resolved_at = 0.0

        player, seen = await self._run(track)

        self.assertEqual(len(seen), 1, "an expired URL was never refreshed")
        self.assertEqual(player.current.source_url, "http://example.invalid/resolved")

    async def test_a_missing_url_is_resolved(self):
        track = _track("Artist - Song")
        track.source_url = ""

        _player, seen = await self._run(track)

        self.assertEqual(len(seen), 1)


class ResolverReusesTheStoredQuery(unittest.TestCase):
    def test_the_resolver_prefers_the_original_query_over_the_display_title(self):
        with open("cogs/music.py", encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("track.search_query or track.title", source)
        self.assertIn("search_query=search_target", source)
        self.assertIn("search_query=st.get(\"query\")", source)


if __name__ == "__main__":
    unittest.main()
