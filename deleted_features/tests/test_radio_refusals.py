"""The refusal sentences the resolver used to write, archived with it.

These two classes lived in ``tests/test_radio.py`` because radio was the fallback
the bad news pointed at — ``_unplayable_hint`` appended *"the radio still works"*
under a provider's refusal, and the refusal sentence itself was the thing a member
read when a search could not be served.

Both halves of that are gone with the extractor: ``/play`` is deleted,
``media_source.MediaError``, ``_chain``, ``source_order`` and the refusal memory
went to ``deleted_features/utils/media_source.py``, and ``_unplayable_hint`` had no
caller left. They are kept here rather than deleted, for the reason the whole
folder exists: the *reasoning* is worth more than the code. Telling a member
"nothing is wrong with your search" instead of "the provider refused this host"
over a request that simply had no recording in it is a distinction any future
resolver will have to make again, and the four cases below are what it took to get
that sentence right.

Archived on 2026-09-22. The suite that covers the resolver itself is
``test_music_matching.py`` beside this file, verbatim.
"""
from __future__ import annotations

import unittest
from unittest import mock

from cogs import music as music_cog
from utils import media_source as M

BLOCKED = "Sign in to confirm you're not a bot"


class TheFailureNamesTheRightCause(unittest.TestCase):
    """A refusal is a fact about the deployment — and not always the whole story.

    The member's card said *"YouTube is refusing this host's address right now"* over
    a request whose real answer was three remixes and no recording. True sentence,
    wrong problem: nothing was wrong with this host's reach, there was simply nothing
    playable to reach.
    """

    def setUp(self):
        # The refusal cooldown is process state on purpose — it is how the source
        # order *learns* — so a test that provokes a refusal has to put it back, or
        # it becomes a finding in whatever test runs next. Learned the hard way: this
        # make-believe YouTube refusal was reported as a real one by the media-health
        # suite's "the verdict carries the doors that are left".
        self._refusals = dict(M._refused_at)

    def tearDown(self):
        M._refused_at.clear()
        M._refused_at.update(self._refusals)

    def _candidate(self, provider, title="Song", duration=240, video_id="x"):
        return M.Candidate(
            provider=provider,
            video_id=f"https://example.invalid/{video_id}",
            title=title,
            duration=duration,
            webpage_url=f"https://example.invalid/{video_id}",
        )

    def _order(self, *providers):
        return [(name, None) for name in providers]

    def test_a_catalogue_of_edits_is_said_out_loud(self):
        def search(_text, provider=None, **_kwargs):
            if provider == "youtube":
                return [self._candidate("youtube", "Song")]
            if provider == "soundcloud":
                # The shape SoundCloud actually answered a mainstream single with:
                # nothing but versions of it, none of them the recording.
                return [
                    self._candidate("soundcloud", "Song (Slowed)"),
                    self._candidate("soundcloud", "Song (Mashup)"),
                ]
            return []

        def stream(track, **_kwargs):
            raise M.MediaError("blocked", BLOCKED, provider=track.provider)

        with mock.patch.object(M, "source_order", return_value=self._order("youtube", "soundcloud", "audius")), \
                mock.patch.object(M, "search", side_effect=search), \
                mock.patch.object(M, "stream", side_effect=stream):
            with self.assertRaises(M.MediaError) as caught:
                M._chain("Song", requester="Raj")

        message = str(caught.exception)
        self.assertEqual(caught.exception.kind, "blocked")
        self.assertIn("refusing this host's address right now", message)
        self.assertIn("nothing but edits, covers or remixes", message)

    def test_sources_that_simply_do_not_have_it_say_so(self):
        def search(_text, provider=None, **_kwargs):
            return [self._candidate("youtube", "Song")] if provider == "youtube" else []

        def stream(track, **_kwargs):
            raise M.MediaError("blocked", BLOCKED, provider=track.provider)

        with mock.patch.object(M, "source_order", return_value=self._order("youtube", "soundcloud", "audius")), \
                mock.patch.object(M, "search", side_effect=search), \
                mock.patch.object(M, "stream", side_effect=stream):
            with self.assertRaises(M.MediaError) as caught:
                M._chain("Song", requester="Raj")

        self.assertIn("the other sources do not have it", str(caught.exception))

    def test_a_refusal_with_no_second_opinion_claims_nothing_about_the_others(self):
        """The clause is a claim, and a claim this code cannot support is worse than
        a shorter sentence. One provider, one refusal, one fact."""

        def search(_text, provider=None, **_kwargs):
            return [self._candidate("youtube", "Song")]

        def stream(track, **_kwargs):
            raise M.MediaError("blocked", BLOCKED, provider=track.provider)

        with mock.patch.object(M, "source_order", return_value=self._order("youtube")), \
                mock.patch.object(M, "search", side_effect=search), \
                mock.patch.object(M, "stream", side_effect=stream):
            with self.assertRaises(M.MediaError) as caught:
                M._chain("Song", requester="Raj")

        message = str(caught.exception)
        self.assertIn("refusing this host's address right now", message)
        self.assertNotIn("other sources", message)

    def test_a_provider_that_had_the_recording_and_failed_elsewhere_adds_no_clause(self):
        """Found it and could not hand it over is a different story, and saying
        *"the others do not have it"* about it would be false."""

        def search(_text, provider=None, **_kwargs):
            return [self._candidate(provider or "youtube", "Song")]

        def stream(track, **_kwargs):
            if track.provider == "youtube":
                raise M.MediaError("blocked", BLOCKED, provider=track.provider)
            raise M.MediaError("no_stream", "that provider gave up", provider=track.provider)

        with mock.patch.object(M, "source_order", return_value=self._order("youtube", "soundcloud")), \
                mock.patch.object(M, "search", side_effect=search), \
                mock.patch.object(M, "stream", side_effect=stream):
            with self.assertRaises(M.MediaError) as caught:
                M._chain("Song", requester="Raj")

        message = str(caught.exception)
        self.assertIn("refusing this host's address right now", message)
        self.assertNotIn("do not have it", message)


class TheWayOutIsOffered(unittest.TestCase):
    """A wall with a door in it, because radio needs no provider at all."""

    def test_the_blocked_reason_points_at_the_thing_that_still_plays(self):
        hint = music_cog._unplayable_hint(M.MediaError("blocked", BLOCKED))
        self.assertIn("/music radio", hint)
        self.assertIn("/music stations", hint)

    def test_every_other_failure_is_left_alone(self):
        for kind in ("not_found", "too_short", "no_stream", "wrong_version"):
            with self.subTest(kind=kind):
                self.assertEqual(music_cog._unplayable_hint(M.MediaError(kind, "x")), "")
