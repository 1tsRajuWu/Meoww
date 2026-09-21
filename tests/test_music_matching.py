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

    def test_an_arrangement_is_a_different_recording(self):
        """Measured rather than guessed. While YouTube refuses this host the second
        provider is the first one asked, and SoundCloud's answer for the studio
        single was ``we can't be friends (wait for your love) [Acoustic Version]``
        — a real recording, and not the one that was asked for."""
        self.assertEqual(
            M.junk_penalty("we can't be friends (acoustic version)", "x"), M.JUNK_PENALTY
        )
        self.assertEqual(M.junk_penalty("song (piano version)", "x"), M.JUNK_PENALTY)

    def test_asking_for_the_arrangement_still_finds_one(self):
        self.assertEqual(M.junk_penalty("song (acoustic)", "artist - song acoustic"), 0.0)

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

        def fake_extract(options, target, **_kwargs):
            # ``require_url`` arrives on a *stream* extraction and not on a search,
            # which is exactly the distinction this test is about — so the stub
            # takes whatever the caller passes rather than pinning the older
            # two-argument shape.
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


class _FakeEngine:
    """A stand-in for ``yt_dlp.YoutubeDL``: answers, or raises, per client set."""

    def __init__(self, answer):
        self._answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, target, download=False):  # noqa: ARG002 — yt-dlp's own signature
        return self._answer(target)


def _client_of(options) -> tuple[str, ...]:
    """The client set an options dict asks for, as :data:`M.CLIENT_LADDER` stores it."""
    args = options.get("extractor_args") or {}
    return tuple((args.get("youtube") or {}).get("player_client") or ())


def _installs_youtube_dl(answer):
    """Patch ``yt_dlp.YoutubeDL`` with a factory that records every attempt.

    Returns ``(seen, patch)`` where ``seen`` fills with the client set of each
    attempt and ``answer(client, target)`` decides what it does — which is how a
    "this host is refused by one client and answered by another" network is built
    without anything touching the real one.
    """
    seen: list[tuple[str, ...]] = []

    def factory(options):
        client = _client_of(options)
        return _FakeEngine(lambda target: (seen.append(client), answer(client, target))[1])

    return seen, mock.patch("yt_dlp.YoutubeDL", side_effect=factory)


class TheExtractorClimbsUntilSomethingAnswers(unittest.TestCase):
    """*"Every song died on the host and played for whoever tested it."*

    That was the production report this exists for: a datacenter IP that YouTube
    refuses, an install that resolves and searches perfectly from a laptop, and a
    bot whose every stream ended in ``DownloadError``. The archived cog had one
    client set and no answer to it. Four behaviours are pinned here, because each
    is a way the climb can be wrong rather than merely absent: it must happen at
    all, it must stop when climbing cannot help, it must not accept an answer that
    is not a stream, and it must not cost a second extraction once a host has
    settled.
    """

    PLAYABLE = {"url": "http://example.invalid/s", "acodec": "opus", "ext": "webm", "abr": 130}

    def setUp(self):
        # Module state, so it has to be put back: a memo of "which client worked"
        # leaking between tests would make the order they run in observable.
        saved = M._last_good_client
        M._last_good_client = None
        self.addCleanup(setattr, M, "_last_good_client", saved)

    def test_a_refused_client_is_followed_by_one_that_answers(self):
        import yt_dlp

        def answer(client, _target):
            if client == ():
                raise yt_dlp.utils.DownloadError(
                    "ERROR: [youtube] abc: Sign in to confirm you're not a bot"
                )
            return dict(self.PLAYABLE)

        seen, patch = _installs_youtube_dl(answer)
        with patch:
            info = M.ytdl_extract(
                M._base_options(flat=False), "https://www.youtube.com/watch?v=abc", require_url=True
            )

        self.assertTrue(info["url"])
        self.assertEqual(seen[0], (), "the configured/default rung must go first")
        self.assertGreater(len(seen), 1, "it never tried a second client set")
        self.assertEqual(M._last_good_client, seen[-1])

    def test_a_network_failure_does_not_climb(self):
        """Four more extractions cannot give a host a route it does not have."""
        import yt_dlp

        def answer(_client, _target):
            raise yt_dlp.utils.DownloadError(
                "ERROR: Unable to download webpage: <urlopen error timed out>"
            )

        seen, patch = _installs_youtube_dl(answer)
        with patch, self.assertRaises(yt_dlp.utils.DownloadError):
            M.ytdl_extract(M._base_options(flat=False), "https://example.invalid/x")

        self.assertEqual(len(seen), 1, "it climbed on a failure climbing cannot fix")

    def test_a_format_list_without_a_url_is_not_an_answer(self):
        """The distinction that makes the climb do any work at all.

        A client set can answer with 47 formats and nothing a streaming player can
        open, because the selected format is a video/audio *pair* that would need
        muxing. That is not a failure and still not a stream.
        """
        # *Every* rung answers with a format list and no URL, which is the case the
        # old code called a success and then handed to the player as no stream.
        def answer(_client, _target):
            return {"title": "x", "formats": [{"format_id": "251"}], "url": None}

        seen, patch = _installs_youtube_dl(answer)
        with patch, self.assertRaises(M.MediaError):
            M.ytdl_extract(
                M._base_options(flat=False), "https://www.youtube.com/watch?v=abc", require_url=True
            )

        self.assertGreater(len(seen), 1, "a format list with no URL was accepted as a stream")
        self.assertEqual(len(seen), len(M.CLIENT_LADDER), "the ladder must be exhausted")

    def test_the_last_good_client_is_asked_for_first_next_time(self):
        """A host that has settled on one client set should not re-learn it per song."""
        M._last_good_client = ("android_vr",)

        def answer(client, _target):
            if client == ():
                raise M.MediaError("no_stream", "the default client did not answer")
            return dict(self.PLAYABLE)

        seen, patch = _installs_youtube_dl(answer)
        with patch:
            M.ytdl_extract(
                M._base_options(flat=False), "https://www.youtube.com/watch?v=abc", require_url=True
            )

        # The rung that last answered is tried *second*, immediately after the
        # caller's own options (which is where an operator's token lives) — so a
        # settled host answers on the first climb rather than re-learning it. The
        # assertion that matters is the one about `seen[1]`: without the memo this
        # is `("tv_embedded",)`, which is the ladder's own order.
        self.assertEqual(seen[0], ())
        self.assertEqual(seen[1], ("android_vr",))


class ASecondProviderIsAskedBeforeAGivingUp(unittest.TestCase):
    """The same song, somewhere else, instead of a dead queue entry.

    The acceptance rule is deliberately *not* the search's confidence margin:
    a rescue already knows which recording it is looking for, so the length is a
    fact about the answer. Without that the rescue inherited the margin, refused
    to choose between four same-named uploads, and returned nothing — silence,
    which is the complaint it exists to answer.
    """

    def _candidate(self, duration, video_id, title="Song", provider="soundcloud"):
        return M.Candidate(
            provider=provider,
            video_id=f"https://soundcloud.com/who/{video_id}",
            title=title,
            duration=duration,
            webpage_url=f"https://soundcloud.com/who/{video_id}",
        )

    def test_the_recording_is_chosen_by_its_length_and_played(self):
        track = M.Track(
            title="Song", provider="youtube", video_id="abc", duration=240,
            expected_artist="Artist", search_query="Artist Song",
        )
        short, right, remix = (
            self._candidate(30, "preview"),
            self._candidate(238, "real"),
            self._candidate(410, "live-version"),
        )
        picked = []

        def fake_pick(candidates, *args, **kwargs):
            picked.extend(candidates)
            return M.Resolution(query="q", track=M.to_track(right, requester="Someone", query="q"))

        with mock.patch.object(M, "_rescue_candidates", return_value=[short, right, remix]), \
                mock.patch.object(M, "pick", side_effect=fake_pick), \
                mock.patch.object(
                    M, "stream",
                    return_value=M.Stream(url="http://cdn/x", ext="mp3", codec="mp3", abr=160.0, duration=238),
                ):
            rescued = M.rescue(track)

        self.assertIsNotNone(rescued)
        self.assertEqual(rescued.provider, "soundcloud")
        self.assertTrue(rescued.stream_url)
        # The card keeps naming what the member asked for.
        self.assertEqual(rescued.title, "Song")
        # …and the preview and the wrong-length upload were never in the running.
        self.assertIn(right, picked)
        self.assertNotIn(short, picked)
        self.assertNotIn(remix, picked)

    def test_nothing_comparable_is_none_rather_than_a_guess(self):
        track = M.Track(title="Song", provider="youtube", video_id="abc", duration=240)
        wrong = M.Candidate(
            provider="soundcloud", video_id="https://soundcloud.com/who/other",
            title="Something Else", duration=95,
        )
        with mock.patch.object(M, "_rescue_candidates", return_value=[wrong]), \
                mock.patch.object(M, "stream", side_effect=AssertionError("must not be reached")):
            self.assertIsNone(M.rescue(track))

    def test_a_track_already_on_the_fallback_provider_is_not_rescued(self):
        """Looking for it again on the host that just refused is not trying harder."""
        track = M.Track(title="Song", provider="soundcloud", video_id="https://soundcloud.com/a/b")
        with mock.patch.object(M, "_rescue_candidates", return_value=[]) as search:
            self.assertIsNone(M.rescue(track))
        search.assert_not_called()

    def test_a_soundcloud_page_is_its_own_identity(self):
        """The numeric id is an *API* id: as a path it resolves to a **user**.

        That is not hypothetical — it is how the first rescue reached a 404 on the
        second provider it was asked for.
        """
        parsed = M.parse("https://soundcloud.com/billieeilish/bittersuite")
        track = M.Track(title="BITTERSUITE", provider="soundcloud", video_id=parsed.video_id)

        self.assertEqual(parsed.video_id, "https://soundcloud.com/billieeilish/bittersuite")
        self.assertEqual(M.watch_url(track), "https://soundcloud.com/billieeilish/bittersuite")

        from_search = M._candidate_from_entry({
            "ie_key": "Soundcloud",
            "id": "1856469195",
            "url": "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A1856469195",
            "webpage_url": "https://soundcloud.com/user/bittersuite",
            "title": "BITTERSUITE",
            "duration": 374.0,
        })
        self.assertEqual(from_search.video_id, "https://soundcloud.com/user/bittersuite")


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


class _FakeResponse:
    """The bits of an ``http.client`` response the catalogue client reads."""

    def __init__(self, payload=b"", content_type="audio/mpeg"):
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class TheCatalogue(unittest.TestCase):
    """The provider that exists because the other two refuse datacenter hosts.

    A refusal that cannot be designed away is a refusal that needs somewhere else
    to go: YouTube answers a flagged address with a bot check, SoundCloud with a
    403, and an operator who has configured neither cookies nor a PO token has run
    out of doors. This one asks for nothing, so it is the door that stays open —
    which is why it is asserted here rather than left to the live hosts to find.
    """

    _ROW = {
        "id": "XbNA7By",
        "title": "BITTERSUITE",
        "duration": 213,
        "play_count": 40000,
        "permalink": "https://audius.co/oliver/bittersuite",
        "user": {"name": "Oliver", "is_verified": True},
        "artwork": {"480x480": "https://cdn.test/art.jpg"},
    }

    def test_the_catalogue_is_asked_without_the_extractor(self):
        """It is not a yt-dlp search, so the pattern here is the JSON, not a prefix."""
        prefixes = dict(M.SEARCH_PROVIDERS)
        self.assertIn("audius", prefixes)
        self.assertEqual(prefixes["audius"], "")

    def test_a_row_becomes_a_candidate_with_the_id_that_re_resolves_it(self):
        with mock.patch.object(M, "_audius_json", return_value={"data": [self._ROW]}):
            found = M.audius_search("bittersuite")
        (candidate,) = found
        self.assertEqual(candidate.provider, "audius")
        self.assertEqual(candidate.video_id, "XbNA7By")
        self.assertEqual(candidate.duration, 213)
        self.assertEqual(candidate.channel, "Oliver")
        self.assertIn("BITTERSUITE", candidate.title)
        self.assertIn("Oliver", candidate.title, "the artist rides the title, as on YouTube")

    def test_a_row_without_an_id_is_not_a_track(self):
        with mock.patch.object(M, "_audius_json", return_value={"data": [{"title": "nameless"}]}):
            self.assertEqual(M.audius_search("nameless"), [])

    def test_the_stream_is_the_id_and_the_answer_is_checked(self):
        track = M.Track(title="BITTERSUITE", provider="audius", video_id="XbNA7By")
        with mock.patch.object(M.urllib.request, "urlopen", return_value=_FakeResponse()) as opened:
            stream = M.stream(track)
        self.assertEqual(stream.ext, "mp3")
        self.assertIn("XbNA7By", stream.url)
        self.assertIn("stream", stream.url)
        self.assertIn("Range", opened.call_args[0][0].headers)

    def test_an_error_page_is_not_a_stream(self):
        """The promise :func:`stream` makes is a playable URL, so an HTML answer has
        to reach the chain as a failure — otherwise the next door is never tried."""
        track = M.Track(title="x", provider="audius", video_id="XbNA7By")
        with mock.patch.object(
            M.urllib.request, "urlopen", return_value=_FakeResponse(b"<html>", "text/html")
        ):
            with self.assertRaises(M.MediaError) as raised:
                M.stream(track)
        self.assertEqual(raised.exception.kind, "no_stream")
        self.assertEqual(raised.exception.provider, "audius")

    def test_a_catalogue_that_does_not_answer_is_a_provider_to_skip(self):
        with mock.patch.object(M, "_audius_json", side_effect=OSError("no route")):
            with self.assertRaises(M.MediaError) as raised:
                M.audius_search("x")
        self.assertEqual(raised.exception.provider, "audius")
        self.assertFalse(M.is_blocked(raised.exception), "a dead host is not a refusal")

    def test_the_ladder_reaches_the_catalogue_when_both_others_refuse(self):
        """The whole point, end to end: the datacenter failure, answered.

        Both hostile providers are made to refuse exactly as they do on a host
        YouTube has flagged, and the search still has to end with a playable track
        rather than a card about a refusal.
        """
        real = M.search

        def hostile(text, *, limit=M.SEARCH_LIMIT, provider=None):
            if provider in ("youtube", "soundcloud"):
                raise M.MediaError("blocked", "Sign in to confirm you're not a bot", provider=provider)
            return real(text, limit=limit, provider=provider)

        with mock.patch.object(M, "search", side_effect=hostile), mock.patch.object(
            M, "_audius_json", return_value={"data": [self._ROW]}
        ), mock.patch.object(M.urllib.request, "urlopen", return_value=_FakeResponse()):
            resolution, provider = M._chain("Oliver BITTERSUITE", requester="RAJ")
        self.assertEqual(provider, "audius")
        self.assertIsNotNone(resolution.track)
        self.assertTrue(resolution.track.moved, "the card has to say it was not the usual source")
        self.assertEqual(resolution.track.found_on, "audius")


class TheFailureThatGetsReported(unittest.TestCase):
    """Which failure a member is shown when every provider failed.

    The old rule was "raise the last one", and it read the order as a sequence of
    increasing information. It is not: the order is a *preference*, and it moves a
    provider that refused to the back — so the failure that ended up last was
    usually a refusal, and the card blamed a platform that had barely been asked.
    """

    def tearDown(self):
        M._refused_at.clear()

    def test_a_refusal_outranks_a_provider_that_simply_found_nothing(self):
        """
        YouTube refuses, SoundCloud answers with an empty list. "Nothing matched" is
        the wrong sentence: something was never asked.
        """

        def search(text, *, limit=M.SEARCH_LIMIT, provider=None):
            if provider == "youtube":
                raise M.MediaError("blocked", "Sign in to confirm you're not a bot", provider=provider)
            return []

        with mock.patch.object(M, "search", side_effect=search):
            with self.assertRaises(M.MediaError) as raised:
                M._chain("anything", requester="RAJ")
        self.assertEqual(raised.exception.kind, "blocked")
        self.assertIn("YouTube", raised.exception.message)
        self.assertEqual(raised.exception.provider, "youtube")

    def test_when_nobody_refused_the_last_failure_is_still_the_answer(self):
        with mock.patch.object(M, "search", return_value=[]):
            with self.assertRaises(M.MediaError) as raised:
                M._chain("nothing at all", requester="RAJ")
        self.assertEqual(raised.exception.kind, "not_found")

    def test_a_refusal_is_never_reported_without_its_platform(self):
        """The card's whole job is to point at the right thing, and "a provider" is
        not a thing. This is the sentence an operator is supposed to act on, so every
        provider the chain can ask has to have a name a reader recognises — including
        any provider added later, which is what this checks."""
        for name, _prefix in M.SEARCH_PROVIDERS:
            self.assertIn(name, M.PROVIDER_LABELS, f"{name} would be reported as itself")
            label = M.PROVIDER_LABELS[name]
            self.assertTrue(label.strip(), name)
            self.assertEqual(label, label[0].upper() + label[1:], f"{name} is not a written name")


class TheClientLadder(unittest.TestCase):
    """A rung is only a rung if the installed extractor knows the client name.

    ``tv_embedded`` was measured as answering once, and then yt-dlp removed it.
    Nothing reported the change, because an unknown client name is not an error —
    it is the absence of one — so the rung became a copy of the first and every
    failing extraction paid for it twice.
    """

    def test_every_rung_is_a_client_this_build_knows(self):
        known = M.known_clients()
        self.assertTrue(known, "the extractor's client table has to be readable")
        for rung in M.client_ladder():
            for name in rung:
                self.assertIn(name, known)

    def test_a_rung_this_build_cannot_honour_is_dropped(self):
        invented = (("a_client_that_does_not_exist",),)
        with mock.patch.object(M, "CLIENT_LADDER", M.CLIENT_LADDER + invented):
            rungs = M.client_ladder()
        self.assertNotIn(invented[0], rungs)
        self.assertIn(M.CLIENT_LADDER[1], rungs, "the real rungs survive the check")

    def test_an_uncheckable_ladder_is_tried_whole_rather_than_emptied(self):
        """An older extractor with a different table should still climb."""
        with mock.patch.object(M, "known_clients", return_value=frozenset()):
            self.assertEqual(M.client_ladder(), M.CLIENT_LADDER)

    def test_the_first_rung_is_still_the_extractors_own_choice(self):
        """It is the only one that negotiates audio-only Opus, so it stays first."""
        self.assertEqual(M.client_ladder()[0], ())


if __name__ == "__main__":
    unittest.main()
