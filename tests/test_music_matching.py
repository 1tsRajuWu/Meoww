"""Music: picking the *right* recording, and saying so when it cannot.

Two production complaints drove the resolver's design, and both were about
identity rather than about search quality.

**"It plays a different song."** The archived cog kept the *query* on each track
and, when a stream URL expired, ran that query again. A search is not a lookup —
it is a fresh result set — so a refresh could land on a different upload than the
card the member was looking at, and it happened mid-song where nobody could see
it. Every track now carries a provider and that provider's own id for the
recording, and re-resolving asks for *that*. It is pinned three ways here: the id
out of a link, the watch URL built back from an id, and the rule that a stored
identity is preferred over the words it was found with.

**"A short clip played."** YouTube happily returns 30-second previews, loops and
nightcore edits, and a duration can match one of those exactly. So the scorer
prefers the artist's own upload, penalises the words that mean *different
recording*, and refuses anything under the floor — and when two results are
genuinely too close to separate, it hands back a shortlist instead of a guess.

None of these tests touch the network: search results are hand-built the way
yt-dlp's flat extraction presents them.
"""
import os
import unittest
from unittest import mock

from utils import media_source as M


def _entry(title, channel, duration, views=0, verified=False, video_id=None, live=False):
    """A yt-dlp flat search result, shaped like the real dict."""
    return M.Candidate(
        provider="youtube",
        video_id=video_id or title.replace(" ", "-")[:11],
        title=title,
        duration=duration,
        channel=channel,
        verified=verified,
        views=views,
        webpage_url=f"https://www.youtube.com/watch?v={video_id or title.replace(' ', '-')[:11]}",
        live=live,
    )


class ALinkIsOneRecording(unittest.TestCase):
    """A link is an identity, not a phrase to search for."""

    def test_a_watch_url_yields_its_video_id(self):
        parsed = M.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RD123")

        self.assertEqual(parsed.kind, "link")
        self.assertEqual(parsed.video_id, "dQw4w9WgXcQ")
        self.assertEqual(parsed.text, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_the_short_form_and_shorts_work_too(self):
        self.assertEqual(M.parse("https://youtu.be/dQw4w9WgXcQ").video_id, "dQw4w9WgXcQ")
        self.assertEqual(
            M.parse("https://www.youtube.com/shorts/dQw4w9WgXcQ").video_id, "dQw4w9WgXcQ"
        )

    def test_words_are_a_search(self):
        parsed = M.parse("Rick Astley - Never Gonna Give You Up")

        self.assertEqual(parsed.kind, "search")
        self.assertEqual(parsed.video_id, "")

    def test_a_spotify_address_is_its_own_kind(self):
        self.assertEqual(M.parse("https://open.spotify.com/track/abc").kind, "spotify")

    def test_an_unknown_address_is_still_a_link(self):
        parsed = M.parse("https://example.invalid/song.mp3")
        self.assertEqual(parsed.kind, "link")
        self.assertEqual(parsed.provider, "url")


class ARefreshAsksForTheSameRecording(unittest.TestCase):
    """The old re-resolution source of the wrong-song bug."""

    def test_the_watch_url_is_built_from_the_id_not_the_title(self):
        track = M.Track(
            title="A Different Title Entirely",
            provider="youtube",
            video_id="dQw4w9WgXcQ",
            search_query="some other words",
        )

        self.assertEqual(M.watch_url(track), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_without_an_id_the_address_it_was_found_at_is_next(self):
        track = M.Track(title="x", provider="url", webpage_url="https://example.invalid/a.mp3")
        self.assertEqual(M.watch_url(track), "https://example.invalid/a.mp3")

    def test_the_words_are_only_the_last_resort(self):
        track = M.Track(title="x", provider="spotify", search_query="Artist - Song")
        self.assertEqual(M.watch_url(track), "Artist - Song")

    def test_identity_names_the_provider_as_well_as_the_id(self):
        self.assertEqual(M.Track(title="x", provider="youtube", video_id="abc").identity, "youtube:abc")
        self.assertEqual(M.Track(title="x", provider="radio", is_radio=True).identity, "")


class StreamsAreOnlyTrustedWhileFresh(unittest.TestCase):
    def test_a_fresh_url_is_reused(self):
        track = M.Track(title="x", stream_url="https://rr1.googlevideo.com/v")
        M.apply_stream(track, M.Stream(url="https://rr1.googlevideo.com/v", is_opus=True, abr=130.0))

        self.assertTrue(track.fresh)
        self.assertTrue(track.is_opus)

    def test_an_unresolved_track_is_never_fresh(self):
        self.assertFalse(M.Track(title="x").fresh)


class NoMoreDowngradedAudio(unittest.TestCase):
    """The format policy is the quality fix, and it is one line."""

    def test_audio_only_opus_is_asked_for_first(self):
        self.assertTrue(M.STREAM_FORMAT.startswith("bestaudio[ext=webm]"))
        self.assertIn("acodec=opus", M.STREAM_FORMAT)

    def test_the_legacy_client_set_is_never_the_default(self):
        for flat in (True, False):
            options = M._base_options(flat=flat)
            self.assertNotIn(
                "extractor_args", options,
                "hardcoding a client set only exposed a muxed 360p MP4",
            )

    def test_the_environment_can_supply_a_token_without_changing_the_default(self):
        with mock.patch.dict(os.environ, {"YTDL_PO_TOKEN": "web+abc"}, clear=False):
            args = M.extractor_args()
        self.assertEqual(args["youtube"]["player_client"], ["web"])
        self.assertTrue(args["youtube"]["po_token"][0].startswith("web."))

    def test_no_token_means_no_extractor_args_at_all(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(M.extractor_args(), {})

    def test_a_cookies_file_is_only_used_when_it_exists(self):
        with mock.patch.dict(os.environ, {"YTDL_COOKIES": "/does/not/exist"}, clear=True):
            self.assertIsNone(M.cookie_file())

    def test_a_base64_cookie_blob_becomes_a_file(self):
        import base64
        import tempfile

        blob = base64.b64encode(b"# Netscape HTTP Cookie File\n").decode()
        with mock.patch.dict(os.environ, {"YTDL_COOKIES_B64": blob}, clear=True):
            try:
                path = M.cookie_file()
            finally:
                os.environ.pop("YTDL_COOKIES_B64", None)
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("meow-ytdl-cookies.txt"))
        os.remove(path)


class MatchingTheRightRecording(unittest.TestCase):
    def test_the_artists_own_upload_beats_a_same_length_reupload(self):
        # The real production case: the album version runs 369s and a re-upload
        # matched it exactly, while the artist's own upload is the 249s radio
        # edit. The old duration-dominated scorer returned the re-upload.
        reupload = _entry(
            "Daft Punk - Get Lucky (Feat. Pharrell Williams)", "EXPO STORY", 369, views=40_000_000
        )
        official = _entry(
            "Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers",
            "Daft Punk", 249, views=900_000_000, verified=True,
        )

        picked = M.pick([reupload, official], "Daft Punk - Get Lucky", 369, "Daft Punk")

        self.assertIsNotNone(picked.track)
        self.assertEqual(picked.track.title, official.title)

    def test_a_slowed_reupload_never_wins(self):
        slowed = _entry(
            "One Dance - Drake | Slowed + Reverbed | Attractive Playlist",
            "Invincible Vibes", 194, views=11_000_000,
        )
        lyrics = _entry(
            "Drake - One Dance (Lyrics) ft. Wizkid & Kyla", "Billion Stars", 175, views=420_000_000
        )

        picked = M.pick([slowed, lyrics], "Drake, Wizkid, Kyla - One Dance", 173, "Drake")

        self.assertEqual(picked.track.title, lyrics.title)

    def test_the_junk_penalty_lowers_the_score_rather_than_raising_it(self):
        # Lower is better. This assertion exists because the first cut subtracted
        # the penalty, which *rewarded* remixes — the live run returned one.
        plain = _entry("Song", "Someone", 200, views=1000)
        remix = _entry("Song (Bonus Remix)", "Someone", 200, views=1000)

        self.assertLess(
            M.score(plain, "Artist - Song", 200),
            M.score(remix, "Artist - Song", 200),
        )

    def test_word_boundaries_stop_false_matches(self):
        self.assertEqual(M.junk_penalty("discover the world", "x"), 0.0)
        self.assertEqual(M.junk_penalty("song (cover)", "x"), M.JUNK_PENALTY)

    def test_a_flavour_the_member_asked_for_is_not_penalised(self):
        self.assertEqual(M.junk_penalty("song (extended remix)", "artist - song remix"), 0.0)

    def test_a_live_take_is_penalised_only_when_it_was_not_asked_for(self):
        live = _entry("Artist - Song (Live at Wembley)", "Artist", 260, views=5_000_000, verified=True)
        studio = _entry("Artist - Song (Official Audio)", "Artist", 200, views=90_000_000, verified=True)

        self.assertEqual(
            M.pick([live, studio], "Artist - Song", 200, "Artist").track.title, studio.title
        )
        self.assertEqual(
            M.pick([live, studio], "Artist - Song live", 260, None).track.title, live.title
        )

    def test_a_fan_channel_mentioning_the_name_is_not_the_artist(self):
        fan = _entry("Song", "Drake Fan Uploads", 200)
        own = _entry("Song", "Drake", 200, verified=True)

        self.assertEqual(M.artist_strength(fan, "Drake"), 1)
        self.assertEqual(M.artist_strength(own, "Drake"), 2)

    def test_vevo_and_topic_feeds_count_as_the_artist(self):
        self.assertEqual(M.artist_strength(_entry("Song", "AdeleVEVO", 200), "Adele"), 2)
        self.assertEqual(M.artist_strength(_entry("Song", "Adele - Topic", 200), "Adele"), 2)

    def test_collaborators_are_reduced_to_the_primary_artist(self):
        self.assertEqual(M.clean_artist("Daft Punk, Pharrell Williams, Nile Rodgers"), "Daft Punk")
        self.assertEqual(M.clean_artist("Tones and I"), "Tones and I")
        self.assertEqual(M.clean_artist("Drake feat. Wizkid"), "Drake")
        self.assertIsNone(M.clean_artist("Unknown Artist"))
        self.assertIsNone(M.clean_artist(""))

    def test_an_artist_can_be_read_out_of_a_plain_query(self):
        self.assertEqual(M.artist_from_query("Rick Astley - Never Gonna Give You Up"), "Rick Astley")
        self.assertIsNone(M.artist_from_query("never gonna give you up"))
        self.assertIsNone(M.artist_from_query("https://youtu.be/abc"))


class WhenItCannotTellItAsks(unittest.TestCase):
    """The whole point of a confidence margin: no confident wrong answers."""

    def test_two_identical_results_produce_a_shortlist_rather_than_a_pick(self):
        first = _entry("Song", "Channel One", 200, views=1000)
        second = _entry("Song", "Channel Two", 200, views=1000)

        resolution = M.pick([first, second], "Song", 200, None)

        self.assertIsNone(resolution.track)
        self.assertEqual(len(resolution.alternatives), 2)
        self.assertTrue(resolution.needs_choice)

    def test_a_clear_winner_is_played_without_asking(self):
        obvious = _entry("Artist - Song (Official Audio)", "Artist", 200, views=900_000_000, verified=True)
        rival = _entry("Some Other Song Entirely", "Nobody", 200)

        resolution = M.pick([obvious, rival], "Artist - Song", 200, "Artist")

        self.assertIsNotNone(resolution.track)
        self.assertEqual(resolution.alternatives, [])
        self.assertFalse(resolution.needs_choice)

    def test_one_result_is_never_ambiguous(self):
        only = _entry("Song", "Somewhere", 200)

        resolution = M.pick([only], "Song", 200, None)

        self.assertIsNotNone(resolution.track)

    def test_a_preview_never_beats_the_full_song(self):
        # A 30-second answer to a song request is a preview. It is still offered
        # when nothing else exists — but the resolution carries it and the
        # command refuses it with words, which is test_pinned below.
        clip = _entry("Song (teaser)", "Channel", 20)
        song = _entry("Song", "Channel", 200)

        self.assertEqual(M.pick([clip, song], "Song", 200, None).track.title, song.title)

    def test_a_forty_five_second_floor_is_the_rule(self):
        self.assertTrue(_entry("x", "c", M.MIN_TRACK_SECONDS - 1).too_short)
        self.assertFalse(_entry("x", "c", M.MIN_TRACK_SECONDS).too_short)

    def test_nothing_usable_resolves_to_nothing(self):
        resolution = M.pick([], "Song", 0, None)

        self.assertIsNone(resolution.track)
        self.assertEqual(resolution.alternatives, [])


class ARefusalIsNamedNotRaisedAtTheMember(unittest.TestCase):
    def test_a_bot_check_is_recognised(self):
        self.assertTrue(M.is_blocked(Exception("HTTP Error 429: Too Many Requests")))
        self.assertTrue(M.is_blocked(Exception("Sign in to confirm you're not a bot")))

    def test_innocent_words_are_not_a_bot_check(self):
        # The archived matcher looked for the substring "bot", which is inside
        # "bootstrap" — so a fixable extraction error was reported as a provider
        # block, sending the reader to the wrong door.
        self.assertFalse(M.is_blocked(Exception("unable to bootstrap the extractor")))
        self.assertFalse(M.is_blocked(Exception("connection reset by peer")))

    def test_every_error_kind_carries_a_sentence(self):
        error = M.MediaError("not_found", "Nothing matched **that**.")

        self.assertEqual(error.kind, "not_found")
        self.assertEqual(error.message, "Nothing matched **that**.")

    def test_an_empty_query_is_refused_without_searching(self):
        with mock.patch.object(M, "available", return_value=True), \
             mock.patch.object(M, "search") as search:
            with self.assertRaises(M.MediaError):
                M.resolve("   ", requester="Raj")
        search.assert_not_called()

    def test_a_missing_extractor_is_reported_rather_than_crashing(self):
        with mock.patch.object(M, "available", return_value=False):
            with self.assertRaises(M.MediaError) as caught:
                M.resolve("anything", requester="Raj")

        self.assertEqual(caught.exception.kind, "extractor_missing")
        self.assertIn("yt-dlp", caught.exception.message)


class TheSearchIsTwoPhasesAndTheSecondOneNeverSearchesAgain(unittest.TestCase):
    def test_a_search_candidate_keeps_the_query_it_was_found_by(self):
        candidate = _entry("Song", "Artist", 200)

        track = M.to_track(candidate, requester="Raj", query="Artist - Song")

        self.assertEqual(track.search_query, "Artist - Song")
        self.assertEqual(track.expected_artist, "Artist")
        self.assertEqual(track.video_id, candidate.video_id)

    def test_resolution_follows_the_identity_not_the_words(self):
        """The bug in one test: a refresh must ask for the recording, not the phrase."""
        track = M.Track(
            title="Song",
            provider="youtube",
            video_id="dQw4w9WgXcQ",
            search_query="Artist - Song",
        )
        seen = []

        def fake_extract(options, target):
            seen.append(target)
            return {"url": "http://example.invalid/s", "acodec": "opus", "ext": "webm", "abr": 130}

        with mock.patch.object(M, "ytdl_extract", side_effect=fake_extract):
            resolved = M.stream(track)

        self.assertEqual(seen, ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
        self.assertTrue(resolved.is_opus)

    def test_a_stream_recorded_on_a_track_is_what_playback_uses(self):
        track = M.Track(title="Song", provider="youtube", video_id="abc")

        M.apply_stream(track, M.Stream(url="http://x", is_opus=True, abr=128.0, duration=200))

        self.assertEqual(track.stream_url, "http://x")
        self.assertTrue(track.is_opus)
        self.assertAlmostEqual(track.abr, 128.0)
        self.assertTrue(track.fresh)


class TheSelfCheckAsksForARealRecording(unittest.TestCase):
    """A version string is not evidence, so the check resolves something.

    Every case here is a *shape of failure* rather than a happy path: the check
    exists because the extractor stays installed, importable and version-reporting
    while answering every search with a bot check, and because the three ways it
    can fail need three different fixes — a missing package, a stale extractor, a
    provider that has flagged this host.
    """

    def test_a_working_extractor_reports_what_it_resolved(self):
        info = {"id": "aqz-KE-bpKQ", "title": "Big Buck Bunny", "duration": 635,
                "webpage_url": M.DEFAULT_PROBE_TARGET, "url": "https://cdn.test/audio",
                "ext": "webm", "acodec": "opus", "abr": 128.0}
        with mock.patch.object(M, "ytdl_extract", return_value=info):
            probe = M.self_check()
        self.assertTrue(probe.healthy)
        self.assertIn("Big Buck Bunny", probe.detail)
        self.assertIn("10:35", probe.detail)
        self.assertEqual(probe.target, M.DEFAULT_PROBE_TARGET)
        self.assertFalse(probe.blocked)
        self.assertIn("working", probe.sentence())

    def test_a_missing_package_is_a_finding_and_not_a_crash(self):
        with mock.patch.object(M, "available", return_value=False), \
                mock.patch.object(M, "ytdl_extract", side_effect=AssertionError("must not be called")):
            probe = M.self_check()
        self.assertFalse(probe.healthy)
        self.assertIn("yt-dlp", probe.reason)
        self.assertFalse(probe.unreachable, "a missing package is not a network problem")

    def test_a_provider_refusal_is_named_as_the_provider_refusing(self):
        refusal = Exception("ERROR: Sign in to confirm you're not a bot. Use --cookies")
        with mock.patch.object(M, "ytdl_extract", side_effect=refusal):
            probe = M.self_check()
        self.assertFalse(probe.healthy)
        self.assertTrue(probe.blocked)
        self.assertFalse(probe.unreachable, "an answer is not a transport failure")
        self.assertIn("refusing this host", probe.reason)

    def test_a_network_with_no_route_out_is_neither_a_finding_nor_a_silence(self):
        with mock.patch.object(
            M, "ytdl_extract", side_effect=Exception("ERROR: Unable to download webpage: <urlopen error timed out>")
        ):
            probe = M.self_check()
        self.assertFalse(probe.healthy)
        self.assertTrue(probe.unreachable)
        self.assertFalse(probe.blocked)
        self.assertIn("could not reach the provider", probe.reason)

    def test_the_recording_asked_for_is_a_setting(self):
        pinned = {"id": "abc", "title": "Mirror", "duration": 200, "url": "https://cdn.test/a",
                  "ext": "webm", "acodec": "opus"}
        with mock.patch.object(M, "ytdl_extract", return_value=pinned) as extract:
            probe = M.self_check("https://mirror.test/watch?v=abc")
        self.assertTrue(probe.healthy)
        self.assertEqual(probe.target, "https://mirror.test/watch?v=abc")
        self.assertEqual(extract.call_args[0][1], "https://mirror.test/watch?v=abc")

    def test_the_default_recording_is_not_a_song_somebody_can_pull(self):
        """A probe naming a music video would start failing when that video is taken
        down, and the check would then be the fault it reports."""
        self.assertIn("youtube.com/watch", M.DEFAULT_PROBE_TARGET)
        self.assertNotIn("music.youtube", M.DEFAULT_PROBE_TARGET)
        self.assertTrue(M.DEFAULT_PROBE_TARGET.startswith("https://"))

    def test_a_probe_that_read_the_recording_but_produced_no_stream_is_a_finding(self):
        info = {"id": "aqz-KE-bpKQ", "title": "Big Buck Bunny", "duration": 635,
                "webpage_url": M.DEFAULT_PROBE_TARGET}
        with mock.patch.object(M, "ytdl_extract", return_value=info):
            probe = M.self_check()
        self.assertFalse(probe.healthy)
        self.assertIn("could not turn it into a stream", probe.reason)


if __name__ == "__main__":
    unittest.main()
