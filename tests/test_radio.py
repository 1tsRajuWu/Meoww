"""The radio catalogue, the search over it, and the memory of what a server chose.

Radio is the whole of playback now, which makes this the suite for the feature
rather than for its fallback — ``tests/test_music.py`` covers the player and
``tests/test_media_health.py`` covers the weekly check over these URLs.

Three things are pinned here, and the first is the one this project keeps re-learning:

* **A catalogue is a set of promises about URLs.** Every station is a stream somebody
  will pick, so a station whose URL is dead is a broken feature discovered by a
  member rather than by us — and the catalogue is therefore held to the shape the
  weekly self-check can probe (:mod:`utils.media_health` scans ``cogs/``, so these
  URLs are in its targets whether or not anybody remembered to add them), plus the
  shape the public ``/music`` page renders.
* **The picker must be a search.** Discord caps fixed choices at 25, so a menu is the
  reason the catalogue was six stations long. That is a *rule*, not a preference: if
  the command ever goes back to ``app_commands.choices``, this catalogue cannot grow.
* **A word a person actually types must find the thing it means.** Nobody searches
  for "focus"; they search for "study".

The refusal sentences this file used to pin — a provider's refusal outranking a
catalogue miss, and the "the radio still works" hint under it — are archived in
``deleted_features/tests/test_radio_refusals.py``, because both halves went with
the extractor: there is no provider left to refuse anything.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import discord

from cogs import music as music_cog
from utils import db, media_health, media_source as M
from utils import style

class TheCatalogue(unittest.TestCase):
    """Every station is a promise, and a promise is checkable."""

    def test_every_station_carries_what_its_two_readers_need(self):
        """The dashboard reads three keys and the commands read the rest.

        ``keep_alive.py`` renders ``name``, ``icon`` and ``desc`` on the public
        ``/music`` page, and the station card renders ``genre``, ``lang`` and
        ``tags``. A station missing any of them is a page with a blank line in it.
        """
        for key, station in music_cog.RADIO_STATIONS.items():
            with self.subTest(station=key):
                for field in ("name", "url", "desc", "icon", "genre"):
                    self.assertTrue(station.get(field), f"{key} has no {field}")
                self.assertIn("tags", station)
                self.assertIn("lang", station)

    def test_the_catalogue_is_a_fallback_rather_than_a_formality(self):
        """Six stations was the version that made the fallback not an answer.

        Radio is what still plays when the media platforms refuse this host, and a
        six-item answer to *"the platforms will not serve you"* leaves most people
        with nothing. The floors are what make that structural instead of a hope.
        """
        stations = music_cog.RADIO_STATIONS.values()
        genres = {station["genre"] for station in stations}
        languages = {station["lang"] for station in stations if station["lang"]}
        instrumental = [
            station for station in stations if "instrumental" in station["tags"]
        ]
        self.assertGreaterEqual(len(music_cog.RADIO_STATIONS), 50)
        self.assertGreaterEqual(len(genres), 20)
        # Raised from 8 in the pass that added one flagship per language. The
        # number is a floor rather than a target: it is what stops the catalogue
        # quietly shrinking back to the languages the maintainer happens to speak.
        self.assertGreaterEqual(len(languages), 20)
        self.assertGreaterEqual(len(instrumental), 25)

    def test_every_declared_language_can_actually_be_searched(self):
        """A `lang` the search cannot match is a station nobody can reach.

        The catalogue grew by *genre* first, which left the one search somebody
        whose language is not this bot's has to be able to make — `/music stations
        Spanish`. A station that exists, carries the tag and does not come back
        from the search is the same experience as a station that is not there, so
        the tag and the ranking are checked against each other rather than
        trusted.
        """
        owners: dict[str, set[str]] = {}
        for key, station in music_cog.RADIO_STATIONS.items():
            if station["lang"]:
                owners.setdefault(station["lang"], set()).add(key)
        for language, keys in sorted(owners.items()):
            found = {
                key
                for key, _station in music_cog.search_stations(language, limit=200)
            }
            with self.subTest(language=language):
                self.assertTrue(
                    keys <= found,
                    f"{language} is declared on {sorted(keys)} but not everyone is "
                    f"findable by it: {sorted(keys - found)}",
                )

    def test_no_mood_outside_the_vocabulary(self):
        """Free-text tags are how a filter quietly stops matching anything.

        The browse command filters on these, so a tag that is not in the vocabulary
        is a tag nobody can search for — which looks exactly like a station that
        does not exist.
        """
        for key, station in music_cog.RADIO_STATIONS.items():
            for tag in station["tags"]:
                self.assertIn(
                    tag, music_cog.RADIO_TAGS, f"{key} is tagged {tag!r}, which is not a mood"
                )

    def test_no_two_stations_share_a_url(self):
        """Two entries on one stream is one station wearing two names."""
        seen: dict[str, str] = {}
        for key, station in music_cog.RADIO_STATIONS.items():
            self.assertNotIn(
                station["url"], seen, f"{key} plays the same stream as {seen.get(station['url'])}"
            )
            seen[station["url"]] = key

    def test_every_stream_is_https(self):
        """Not a security rule so much as a deployability one: an http stream is a
        station that stops working the day anything in front of it insists."""
        for key, station in music_cog.RADIO_STATIONS.items():
            with self.subTest(station=key):
                self.assertTrue(station["url"].startswith("https://"), station["url"])

    def test_the_self_check_would_probe_every_one_of_them(self):
        """The durable half: every station is in the weekly probe without being added.

        ``utils/media_health.scan_source`` reads hardcoded URLs out of ``cogs/``, so
        most of the catalogue is covered by *being written where it is*. The rest are
        **not**, and this is the defect that produced
        ``media_health._unscannable_stations``: a radio stream frequently has no file
        extension, so ``https://stream.laut.fm/lofi`` looks like a host and a word,
        and the scan skipped 91 of the 122 stations — a dead link nothing checks,
        because nothing recognised it as a link.

        The law is about :func:`utils.media_health.targets`, which is what one pass
        actually asks about, rather than about either half of it.
        """
        probed = {target.url for target in media_health.targets() if target.url}
        missing = [
            station["url"]
            for station in music_cog.RADIO_STATIONS.values()
            if station["url"] not in probed
        ]
        self.assertEqual(missing, [], f"not probed by the self-check: {missing}")

    def test_a_station_is_never_probed_twice(self):
        """Declared and scanned are two routes to the same URL, and a URL asked
        about twice is a count that overstates how much of the catalogue was
        checked — the one number this whole module exists to report honestly."""
        seen = [target.url for target in media_health.targets() if target.url]
        self.assertEqual(len(seen), len(set(seen)))


class SearchingTheCatalogue(unittest.TestCase):
    """A hundred-odd stations are unusable behind a menu, so the search is the feature."""

    def test_a_genre_finds_its_own_stations_first(self):
        found = [key for key, _station in music_cog.search_stations("jazz")]
        self.assertEqual(found[:3], ["jazz", "jazzcafe", "jazzfm"])

    def test_a_word_people_type_finds_the_mood_it_means(self):
        """*"Can I have more instrumental music types"* — and nobody types the tag.

        This is the whole reason the synonym table exists: the catalogue says
        ``focus`` because that is what the tag means, and a member types *study*
        because that is what they are doing.
        """
        found = music_cog.search_stations("study")
        self.assertTrue(found, "searching for study found nothing")
        self.assertTrue(all("focus" in station["tags"] for _key, station in found))

    def test_a_synonym_never_displaces_a_literal_match(self):
        """Otherwise the synonym table becomes a search nobody can predict.

        A station whose *genre* is Jazz is a better answer to ``jazz`` than one whose
        description mentions jazz, and that ordering has to hold no matter how many
        synonyms point at the same tag.
        """
        found = music_cog.search_stations("jazz")
        self.assertEqual(found[0][1]["genre"], "Jazz")

    def test_it_can_be_searched_by_language_and_by_mood(self):
        for word, check in (
            ("japanese", lambda s: s["lang"] == "Japanese"),
            ("instrumental", lambda s: "instrumental" in s["tags"]),
            ("Turkish", lambda s: s["lang"] == "Turkish"),
        ):
            with self.subTest(query=word):
                found = music_cog.search_stations(word)
                self.assertTrue(found, f"{word} matched nothing")
                self.assertTrue(any(check(station) for _key, station in found))

    def test_an_empty_search_is_the_catalogue_in_order(self):
        found = music_cog.search_stations("", limit=5)
        keys = list(music_cog.RADIO_STATIONS)[:5]
        self.assertEqual([key for key, _station in found], keys)

    def test_nothing_matching_is_an_empty_answer_not_a_guess(self):
        self.assertEqual(music_cog.search_stations("zzzznotastation"), [])
        self.assertIsNone(music_cog.station_for("zzzznotastation"))

    def test_a_station_is_found_by_its_key_its_name_or_part_of_either(self):
        self.assertEqual(music_cog.station_for("lofi")[0], "lofi")
        self.assertEqual(music_cog.station_for("Lofi Beats")[0], "lofi")
        self.assertEqual(
            music_cog.station_for(music_cog.RADIO_STATIONS["bollywood"]["name"])[0],
            "bollywood",
        )
        self.assertIsNone(music_cog.station_for(""))


class ThePickerIsASearch(unittest.TestCase):
    """The rule that let the catalogue grow past six."""

    def _radio(self):
        group = next(
            command
            for command in music_cog.Music.__cog_app_commands__
            if command.name == "music"
        )
        return next(child for child in group.commands if child.name == "radio"), group

    def test_the_station_argument_is_autocompleted_and_not_a_fixed_menu(self):
        radio, _group = self._radio()
        parameter = next(p for p in radio.parameters if p.name == "station")
        self.assertEqual(
            parameter.choices,
            [],
            "a fixed choice list is capped at 25 by Discord and cannot hold this catalogue",
        )
        self.assertIsNotNone(parameter.autocomplete, "the station option has no autocomplete")
        self.assertTrue(parameter.required is False, "the station should default")

    def test_a_browsable_command_exists_for_when_you_do_not_know_a_word(self):
        _radio, group = self._radio()
        self.assertIn("stations", {child.name for child in group.commands})

    def test_a_suggestion_fits_in_a_discord_label_and_names_a_real_station(self):
        """Discord rejects a label over 100 characters and a value over 100 too."""
        for key, station in music_cog.RADIO_STATIONS.items():
            label = music_cog.station_option_label(key, station)
            self.assertLessEqual(len(label), 100, label)
            self.assertLessEqual(len(key), 100, key)
            self.assertIn(station["name"], label)

    def test_the_suggestions_stay_inside_discords_ceiling(self):
        self.assertLessEqual(len(music_cog.search_stations("a", limit=200)), 200)
        self.assertLessEqual(
            len(music_cog.search_stations("", limit=music_cog.STATION_SUGGESTIONS)),
            music_cog.STATION_SUGGESTIONS,
        )


class RememberingTheStation(unittest.IsolatedAsyncioTestCase):
    """`/music radio` with no argument is the common case, not the exotic one."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._original = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp.name, "radio.db")
        await db.setup()

    async def asyncTearDown(self):
        db.DB_PATH = self._original
        self.temp.cleanup()

    async def test_a_server_that_has_chosen_nothing_has_chosen_nothing(self):
        """Not a default dressed up as a choice — the caller picks and says so."""
        self.assertEqual(await db.get_radio_station(1234), "")

    async def test_the_last_station_comes_back_and_the_second_one_replaces_it(self):
        await db.set_radio_station(99, "jazz")
        self.assertEqual(await db.get_radio_station(99), "jazz")
        await db.set_radio_station(99, "kpop")
        self.assertEqual(await db.get_radio_station(99), "kpop")
        self.assertEqual(
            await db.get_radio_station(100), "", "a guild read another guild's station"
        )

    def test_every_remembered_key_still_names_a_station(self):
        """A stored key that no longer exists must not be a crash or a blank card.

        The catalogue will change — a station dies, gets renamed, gets dropped. What
        must not happen is a server whose remembered choice stops resolving.
        """
        for key in music_cog.RADIO_STATIONS:
            with self.subTest(station=key):
                self.assertIsNotNone(music_cog.station_for(key))


class TheStationCard(unittest.TestCase):
    """It says genre, language and mood, because that is what a catalogue buys."""

    def test_it_names_the_station_the_room_and_the_kind_of_music(self):
        body = music_cog._station_card(
            music_cog.RADIO_STATIONS["kpop"], "<#1>", "Raj"
        )
        self.assertIn("K-Pop", body)
        self.assertIn("Korean", body)
        self.assertIn("Raj", body)

    def test_it_says_when_the_choice_was_the_server_s_own_earlier_one(self):
        from_memory = music_cog._station_card(
            music_cog.RADIO_STATIONS["lofi"], "<#1>", "Raj", from_memory=True
        )
        fresh = music_cog._station_card(music_cog.RADIO_STATIONS["lofi"], "<#1>", "Raj")
        self.assertNotEqual(from_memory, fresh)
        self.assertIn("last station", from_memory.lower())


class TheRadioCommand(unittest.TestCase):
    """The memory in use: the same station back, and a random one that says so."""

    def _run(self, station_argument, remembered):
        from types import SimpleNamespace

        cog = music_cog.Music.__new__(music_cog.Music)
        cog.bot = SimpleNamespace()

        sent: list[str] = []
        played: list[str] = []

        class _Channel:
            mention = "<#7>"

        class _Voice:
            channel = _Channel()

            def is_playing(self):
                return False

            def is_paused(self):
                return False

            def stop(self):
                played.append("stop")

        fake_player = SimpleNamespace(
            queue=[],
            voice_client=None,
            text_channel=None,
            is_247=False,
            current=None,
        )

        async def _play_next():
            played.append(fake_player.queue[-1].title if fake_player.queue else "")

        fake_player.play_next = _play_next
        cog.get_player = lambda _guild_id: fake_player

        async def _ensure_voice(_interaction):
            fake_player.voice_client = _Voice()
            return fake_player.voice_client

        cog._ensure_voice = _ensure_voice
        cog._unplayable = lambda: None

        interaction = SimpleNamespace(
            guild_id=5,
            user=SimpleNamespace(display_name="Raj"),
            channel=_Channel(),
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )

        async def _send_card(_interaction, content=None, **_kwargs):
            sent.append(content or "")

        set_mock = mock.AsyncMock()
        patches = [
            mock.patch.object(style, "send_card", side_effect=_send_card),
            mock.patch.object(music_cog, "ControlsView", return_value=None),
            mock.patch.object(db, "get_radio_station", mock.AsyncMock(return_value=remembered)),
            mock.patch.object(db, "set_radio_station", set_mock),
            mock.patch.object(db, "get_247_setting", mock.AsyncMock(return_value=(False, 0))),
        ]
        import asyncio

        for patch in patches:
            patch.start()
        try:
            asyncio.run(cog.radio.callback(cog, interaction, station_argument))
        finally:
            for patch in reversed(patches):
                patch.stop()
        return sent, played, set_mock

    def test_no_argument_replays_what_this_server_chose_last(self):
        sent, played, remembered = self._run(None, "kpop")
        self.assertIn("K-Pop", sent[0])
        self.assertIn("last station", sent[0])
        remembered.assert_awaited_with(5, "kpop")

    def test_the_first_ever_invocation_picks_one_and_does_not_pretend_it_was_asked_for(self):
        sent, _played, remembered = self._run(None, "")
        self.assertTrue(sent, "nothing was said")
        self.assertNotIn("last station", sent[0])
        self.assertTrue(remembered.await_count == 1)
        self.assertIn(remembered.await_args.args[1], music_cog.RADIO_STATIONS)

    def test_a_named_station_wins_over_the_memory(self):
        sent, _played, remembered = self._run("salsa", "kpop")
        self.assertIn("Salsa", sent[0])
        self.assertNotIn("last station", sent[0])
        remembered.assert_awaited_with(5, "salsa")

    def test_a_name_that_does_not_exist_is_refused_rather_than_guessed(self):
        """A station that does not exist must not silently become the default one."""
        from types import SimpleNamespace as _NS

        cog = music_cog.Music.__new__(music_cog.Music)
        cog.bot = _NS()
        cog._unplayable = lambda: None
        sent: list[str] = []

        async def _send_card(_interaction, content=None, **_kwargs):
            sent.append(content or "")
            return None

        class _Response:
            async def send_message(self, content=None, **kwargs):
                sent.append(content or "")

            async def defer(self, **_kwargs):
                sent.append("<deferred>")

        interaction = _NS(response=_Response(), guild_id=5)
        import asyncio

        with mock.patch.object(style, "send_card", side_effect=_send_card):
            asyncio.run(cog.radio.callback(cog, interaction, "qqzzxx"))
        self.assertIn("don't have a station called", sent[0])
        self.assertNotIn("<deferred>", sent, "it started a station anyway")


class TheBrowseCommand(unittest.TestCase):
    """It answers the question a search box cannot: what *is* there?"""

    def _run(self, query):
        from types import SimpleNamespace

        cog = music_cog.Music.__new__(music_cog.Music)
        captured: dict[str, object] = {}

        async def _send_card(_interaction, content=None, embed=None, **_kwargs):
            captured["content"] = content
            captured["embed"] = embed

        interaction = SimpleNamespace(guild_id=5, response=SimpleNamespace())
        import asyncio

        with mock.patch.object(style, "send_card", side_effect=_send_card):
            asyncio.run(cog.stations.callback(cog, interaction, query))
        return captured

    def test_with_no_word_it_lists_the_genres_and_how_many_of_each(self):
        captured = self._run(None)
        embed: discord.Embed = captured["embed"]
        self.assertIn(str(len(music_cog.RADIO_STATIONS)), embed.description)
        self.assertIn("**Chill**", embed.description)

    def test_with_a_word_it_lists_the_stations_that_word_means(self):
        captured = self._run("instrumental")
        embed: discord.Embed = captured["embed"]
        self.assertIn("Instrumental", embed.description)
        self.assertIn("instrumental, focus", embed.description)

    def test_a_word_that_matches_nothing_says_so_without_inventing_a_result(self):
        captured = self._run("zzzznotastation")
        self.assertIn("Nothing in the catalogue matches", captured["content"])


if __name__ == "__main__":
    unittest.main()
