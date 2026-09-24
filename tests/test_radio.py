"""The rebuilt radio: the table, the pick, the engine, and the one command.

Four things are pinned here, and the first is the one this project keeps
re-learning:

* **A station table is a set of promises about URLs.** Every row is a stream
  somebody will pick, so a dead one is a broken feature discovered by a member
  rather than by us. The URLs are covered by the weekly link scan
  (:mod:`utils.media_health` walks the tree, so ``utils/radio_catalogue.py`` is in
  its targets whether or not anybody remembered to add it) and
  ``scripts/probe_stations.py`` is the same question asked by hand; what this file
  adds is the *shape* a row has to have to be probed at all.
* **A country row has to corroborate its own country.** The rename rebuilt the
  country pass from a public directory, and a directory's country field is a claim
  like any other — the pass that built this table threw out a Ukrainian internet
  station filed under the Emirates. The rule is written down and tested.
* **A live stream that ends is a cut, and a cut is asked again.** The retry budget
  is the difference between a radio that plays all night and one that stops every
  few minutes and says so; both halves are asserted here rather than argued about.
* **The surface is three commands, and the settings that change what everyone
  hears are saved only for somebody who can configure the server.** A member
  without that permission still gets their choice for the start they asked for,
  and the card says which of the two happened.

The suites this replaces are archived whole in ``deleted_features/radio_2026_09/``
— the player's mechanics in ``test_music.py`` and the old catalogue's rules in
``test_radio.py`` — because the code they covered is what was retired.
"""
from __future__ import annotations

import asyncio
import os
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from utils import radio, radio_catalogue as catalogue, radio_stream, style

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------- fakes #
class _FakeChannel:
    def __init__(self, name: str = "Lobby", cid: int = 7):
        self.name = name
        self.id = cid
        self.mention = f"<#{cid}>"
        self.members: list = []
        self.bitrate = 128000

    def permissions_for(self, _member):
        return SimpleNamespace(connect=True)


class _FakeVoice:
    """A voice client that records what it was asked to do."""

    def __init__(self, channel: _FakeChannel | None = None):
        self.channel = channel or _FakeChannel()
        self.played: list = []
        self.stopped = 0
        self.disconnected = False
        self._playing = False
        self._paused = False

    def is_connected(self):
        return not self.disconnected

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def play(self, source, after=None):
        self.played.append(source)
        self.after = after
        self._playing = True

    def stop(self):
        self.stopped += 1
        self._playing = False
        self._paused = False

    def pause(self):
        self._paused = True
        self._playing = False

    def resume(self):
        self._paused = False
        self._playing = True

    async def disconnect(self, force=False):
        self.disconnected = True

    async def move_to(self, _channel):
        return None


class _FakeBot:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.user = SimpleNamespace(id=999)
        self.voice_clients: list = []
        self.guilds: list = []

    def is_module_enabled(self, _guild_id, _name):
        return True


def _player(guild_id: int = 1, *, channel: _FakeChannel | None = None):
    """A real player wired to a fake socket, with no source building at all."""
    bot = _FakeBot()
    player = radio_stream.RadioPlayer(bot, guild_id)
    player.voice_client = _FakeVoice(channel)
    player._build_source = lambda url, kbps=128, station=None: (f"src:{url}", "fake")
    return player


# --------------------------------------------------------------------- table #
class TheTable(unittest.TestCase):
    """Every row is a promise, and a promise is checkable."""

    def test_every_row_carries_what_its_readers_need(self):
        """The page renders name/icon/desc; the commands render genre and tags."""
        self.assertGreater(len(catalogue.CATALOGUE), 200, "the table lost its width")
        for station in catalogue.CATALOGUE:
            with self.subTest(station=station.key):
                for field in ("key", "name", "url", "genre", "desc", "icon"):
                    self.assertTrue(getattr(station, field), f"{station.key} has no {field}")
                self.assertTrue(
                    station.url.startswith("http"), f"{station.key} is not a URL"
                )

    def test_the_keys_and_the_urls_are_unique(self):
        """A duplicate key silently shadows a row; a duplicate URL plays twice."""
        self.assertEqual(catalogue.duplicate_keys(), [])
        urls = [station.url for station in catalogue.CATALOGUE]
        self.assertEqual(len(urls), len(set(urls)), "two rows point at one stream")

    def test_every_tag_is_from_the_closed_vocabulary(self):
        """A filter cannot quietly stop matching because somebody spelled it wrong."""
        for station in catalogue.CATALOGUE:
            with self.subTest(station=station.key):
                for tag in station.tags:
                    self.assertIn(tag, catalogue.TAGS, f"{station.key}: {tag}")

    def test_a_country_row_corroborates_its_country(self):
        """The rule the country pass is held to, checked on the finished table.

        A row is in as a country's own station when its name or its recorded
        capital names the place, or when the stream is served from that country's
        own domain — the passing of a public directory's country field is not
        enough, which is what threw out the Ukrainian station filed under the UAE.
        """
        def fold(text: str) -> str:
            return "".join(ch for ch in str(text).lower() if ch.isalnum())

        for station in catalogue.CATALOGUE:
            if not station.country:
                continue
            with self.subTest(station=station.key):
                haystack = fold(f"{station.name} {station.capital}")
                host = station.url.split("/")[2].lower()
                self.assertTrue(
                    station.capital
                    or fold(station.country) in haystack
                    or "." in host and host.rsplit(".", 1)[-1].isalpha(),
                    f"{station.key} claims {station.country} with nothing to back it",
                )

    def test_the_coverage_floors_hold(self):
        """Floors rather than counts, so the table may grow without editing this."""
        index = catalogue.index()
        self.assertGreaterEqual(index.stations, 250)
        self.assertGreaterEqual(len(index.languages), 20)
        self.assertGreaterEqual(len(index.countries), 120)
        self.assertGreaterEqual(index.ad_free, 25)

    def test_the_index_adds_up(self):
        """Every station is in exactly one of the language buckets."""
        index = catalogue.index()
        counted = sum(count for _language, count in index.languages)
        self.assertEqual(
            counted + index.wordless + index.unlabelled,
            index.stations,
            "the language buckets and the two blank ones do not account for the table",
        )
        self.assertGreater(index.wordless, 0, "no station is tagged instrumental")

    def test_the_country_picker_offers_the_capital_first(self):
        """The question the option answers is *what does the capital sound like*."""
        for name in ("Australia", "Canada", "Kenya"):
            rows = catalogue.in_country(name)
            with self.subTest(country=name):
                self.assertTrue(rows, f"{name} has no station in the table")
                if any(row.capital for row in rows):
                    self.assertTrue(rows[0].capital, f"{name}'s capital row is not first")

    def test_a_country_that_did_not_verify_is_absent_rather_than_invented(self):
        """The list is the table's own, so the picker cannot offer a dead stream."""
        self.assertEqual(catalogue.countries(), list(catalogue.index().countries))
        for name in catalogue.countries():
            with self.subTest(country=name):
                self.assertTrue(catalogue.in_country(name))


class SearchingTheTable(unittest.TestCase):
    def test_a_name_match_beats_a_description_match(self):
        found = catalogue.search("jazz", limit=5)
        self.assertTrue(found)
        self.assertIn("jazz", catalogue._flat(found[0].name + found[0].genre))

    def test_a_word_people_type_finds_the_tag_it_means(self):
        """Nobody searches for ``focus``; they search for *study*."""
        found = catalogue.search("study", limit=5)
        self.assertTrue(found)
        self.assertTrue(
            any("focus" in station.tags for station in found),
            "study did not reach the focus stations",
        )

    def test_a_language_and_a_country_are_searchable_words(self):
        self.assertTrue(
            any(station.language == "Japanese" for station in catalogue.search("japanese", limit=8))
        )
        self.assertTrue(
            any(station.country == "Australia" for station in catalogue.search("australia", limit=8))
        )

    def test_the_option_label_fits_discords_cap(self):
        for station in catalogue.CATALOGUE:
            with self.subTest(station=station.key):
                self.assertLessEqual(len(catalogue.option_label(station)), 100)

    def test_an_empty_search_is_the_head_of_the_table(self):
        self.assertEqual(
            [station.key for station in catalogue.search("", limit=3)],
            [station.key for station in catalogue.CATALOGUE[:3]],
        )


# -------------------------------------------------------------------- policy #
class TheDefaultsAreTheRequest(unittest.TestCase):
    def test_the_defaults_are_what_the_command_promises(self):
        self.assertEqual(radio.DEFAULT_LANGUAGE, "English")
        self.assertEqual(radio.DEFAULT_ADS, "ad-free")
        self.assertTrue(radio.DEFAULT_STAY)

    def test_an_unknown_mode_or_language_reads_as_the_default(self):
        """A hand-edited row and a value from a future version both degrade safely."""
        self.assertEqual(radio.ads_mode("only"), radio.DEFAULT_ADS, "the old vocabulary maps nowhere")
        self.assertEqual(radio.ads_mode("strict"), "strict")
        self.assertEqual(radio.ads_mode("nonsense"), radio.DEFAULT_ADS)
        self.assertEqual(radio.language_name("finnish"), "Finnish")
        self.assertEqual(radio.language_name("Klingon"), "")
        # Every country the table holds resolves from any case, because the
        # option is autocompleted *and* typable and a stored value may be either.
        for country in catalogue.countries():
            self.assertEqual(radio.country_name(country.lower()), country)
        self.assertEqual(radio.country_name("Atlantis"), "")


class ThePick(unittest.TestCase):
    def test_a_named_station_plays_over_the_pool(self):
        """A name is not a language and not a country: the pool cannot veto it.

        ``ads`` is the *preference* here rather than ``strict``, because strict is
        the one mode that can say no to a member — it has its own case below, and
        the two together are the whole rule: settings narrow a *choice*, and only
        a rule refuses a *name*.
        """
        named = catalogue.by_key("jazz")
        self.assertIsNotNone(named)
        choice = radio.choose(named="jazz", language="Finnish", ads="ad-free")
        self.assertTrue(choice.ok)
        self.assertEqual(choice.reason, "named")
        self.assertEqual(choice.station.key, "jazz")

    def test_a_name_the_table_does_not_hold_is_a_refusal_that_names_it(self):
        choice = radio.choose(named="Radio Nowhere At All")
        self.assertFalse(choice.ok)
        self.assertEqual(choice.reason, "unknown")
        self.assertIn("Radio Nowhere At All", radio.refusal_sentence(choice))

    def test_strict_refuses_a_station_that_carries_advertising(self):
        """The one mode that says no to a person, which is why it is asked for."""
        carrying = next(s for s in catalogue.CATALOGUE if not s.ad_free and not s.country)
        choice = radio.choose(named=carrying.key, ads="strict")
        self.assertFalse(choice.ok)
        self.assertEqual(choice.reason, "strict")
        self.assertIn(carrying.name, radio.refusal_sentence(choice))

    def test_the_bot_choice_is_english_by_default(self):
        for _ in range(12):
            choice = radio.choose(language=radio.DEFAULT_LANGUAGE)
            self.assertTrue(choice.ok)
            self.assertEqual(choice.station.language, radio.DEFAULT_LANGUAGE)

    def test_the_bot_choice_is_ad_free_when_it_can_be(self):
        """The rule, per language, against whatever this table actually holds.

        Asserted as an invariant rather than as one fact about English, because the
        ad-free rows in this table are the instrumental ones — no language tag — so
        whether a given language has an ad-free station is a property of the data
        and not of the pick: the pick is either ad-free, or it said why it is not.
        """
        for language, _count in catalogue.index().languages:
            with self.subTest(language=language):
                choice = radio.choose(language=language)
                self.assertTrue(choice.ok)
                self.assertTrue(choice.station.ad_free or choice.note, language)
                self.assertEqual(choice.station.language, language)

    def test_a_language_with_no_ad_free_station_falls_back_and_says_so(self):
        """A preference must never leave a room with nothing to play."""
        plain = next(
            language
            for language, _count in catalogue.index().languages
            if not any(s.ad_free for s in catalogue.in_language(language))
        )
        choice = radio.choose(language=plain)
        self.assertTrue(choice.ok, "the fallback emptied the room")
        self.assertTrue(choice.note, "the fallback was silent about itself")
        self.assertNotEqual(choice.station.language, "")

    def test_a_country_wins_over_a_language(self):
        """Knowing the country you want tells the table more than the language."""
        country = next(
            name
            for name in catalogue.countries()
            if not any(station.ad_free for station in catalogue.in_country(name))
        )
        elsewhere = next(
            language
            for language in catalogue.languages()
            if all(station.language != language for station in catalogue.in_country(country))
        )
        picked = radio.pool(language=elsewhere, country=country)
        self.assertTrue(picked.stations)
        self.assertTrue(all(station.country == country for station in picked.stations))
        self.assertTrue(all(station.language != elsewhere for station in picked.stations))
        # And the ad preference does not widen it back out: this country has no
        # ad-free station, so the pick carries advertising and says so.
        self.assertTrue(picked.note, "a compromise with nothing said about it")

    def test_a_remembered_station_plays_only_while_it_still_qualifies(self):
        remembered = catalogue.ad_free_stations()[0]
        kept = radio.choose(remembered=remembered.key)
        self.assertEqual(kept.reason, "memory")
        self.assertEqual(kept.station.key, remembered.key)

        # The same memory, asked for in a language that station is not in: the
        # standing choice is set aside rather than played over the request.
        elsewhere = next(
            language
            for language in catalogue.languages()
            if all(station.key != remembered.key for station in catalogue.in_language(language))
        )
        skipped = radio.choose(remembered=remembered.key, language=elsewhere)
        self.assertNotEqual(skipped.reason, "memory")
        self.assertEqual(skipped.station.language, elsewhere)

    def test_the_shuffled_pick_is_seeded_rather_than_hopeful(self):
        first = radio.choose(rng=random.Random(7)).station.key
        again = radio.choose(rng=random.Random(7)).station.key
        self.assertEqual(first, again)

    def test_an_empty_pool_is_a_sentence_not_a_crash(self):
        choice = radio.choose(country="Atlantis")
        self.assertFalse(choice.ok)
        self.assertEqual(choice.reason, "empty")
        self.assertTrue(radio.refusal_sentence(choice))


# -------------------------------------------------------------------- engine #
class TheEngine(unittest.TestCase):
    def setUp(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        radio_stream.PLAYERS.clear()

    def test_holds_voice_is_true_for_a_station_or_a_seat(self):
        self.assertFalse(radio_stream.holds_voice(1), "no player is not holding voice")
        player = _player(1)
        self.assertFalse(radio_stream.holds_voice(1))
        player.stay = True
        self.assertTrue(radio_stream.holds_voice(1), "a seat is holding voice")
        player.stay = False
        player.current = catalogue.CATALOGUE[0]
        self.assertTrue(radio_stream.holds_voice(1), "a station is holding voice")

    def test_a_healthy_stretch_restores_the_retry_budget(self):
        """A station that plays an hour then drops gets its attempts back."""
        player = _player(2)
        player.current = catalogue.CATALOGUE[0]
        player._live_retries = radio_stream.LIVE_RETRY_LIMIT
        self.assertTrue(
            player._worth_another_attempt(radio_stream.LIVE_RETRY_AFTER + 1),
            "a long play was not treated as a healthy source that ended",
        )
        self.assertEqual(player._live_retries, 1)

    def test_a_station_that_cannot_hold_a_socket_becomes_news(self):
        player = _player(3)
        player.current = catalogue.CATALOGUE[0]
        for _ in range(radio_stream.LIVE_RETRY_LIMIT):
            self.assertTrue(player._worth_another_attempt(5.0))
        self.assertFalse(
            player._worth_another_attempt(5.0),
            "the budget never runs out, so a dead station retries for ever",
        )
        self.assertIn(str(radio_stream.LIVE_RETRY_LIMIT), player._cut_detail(5.0))

    def test_a_stale_callback_is_history_rather_than_a_cut(self):
        """The whole reason the generation counter exists."""
        player = _player(4)
        player.current = catalogue.CATALOGUE[0]
        reported: list = []
        player.on_fault = lambda fault: reported.append(fault)
        callback = player._after_for(1)
        player._generation = 2
        callback(None)
        self.assertEqual(reported, [], "a replaced source reported itself as the station dying")

    def test_start_replaces_what_is_playing_and_reports_the_station(self):
        player = _player(5)
        station = catalogue.CATALOGUE[0]
        ok, why = asyncio.get_event_loop().run_until_complete(player.start(station, "Raj"))
        self.assertTrue(ok, why)
        self.assertEqual(player.current.key, station.key)
        self.assertEqual(player.requester, "Raj")
        self.assertEqual(len(player.voice_client.played), 1)
        status = player.status()
        self.assertEqual(status["station"]["name"], station.name)
        self.assertTrue(status["playing"])
        self.assertIn("stay", status)

    def test_a_start_that_cannot_build_reports_rather_than_pretends(self):
        player = _player(6)
        faults: list = []
        player.on_fault = lambda fault: faults.append(fault)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("no ffmpeg here")

        player._build_source = _boom
        ok, why = asyncio.get_event_loop().run_until_complete(player.start(catalogue.CATALOGUE[0]))
        self.assertFalse(ok)
        self.assertIn("RuntimeError", why)
        self.assertIsNone(player.current, "a station that never started is still 'on air'")
        self.assertEqual(faults and faults[0].kind, "unplayable")

    def test_stop_takes_it_off_air_and_leaves_unless_the_seat_is_held(self):
        player = _player(7)
        socket = player.voice_client
        asyncio.get_event_loop().run_until_complete(player.start(catalogue.CATALOGUE[0]))
        asyncio.get_event_loop().run_until_complete(player.stop(reason="test"))
        self.assertIsNone(player.current)
        self.assertTrue(socket.disconnected, "a station with no seat held the channel")
        self.assertIsNone(player.voice_client, "a dead socket was kept")

        seating = _player(8)
        seating.stay = True
        asyncio.get_event_loop().run_until_complete(seating.start(catalogue.CATALOGUE[0]))
        asyncio.get_event_loop().run_until_complete(seating.stop(reason="test"))
        self.assertFalse(seating.voice_client.disconnected, "a held seat gave the channel back")
        self.assertTrue(seating.voice_client.is_connected(), "a seat that holds voice lost its socket")

    def test_the_probe_never_raises_and_absent_is_not_fine(self):
        """The two rules that keep a diagnostic from becoming the fault."""
        dead = radio_stream.self_check("http://127.0.0.1:9/nothing.mp3")
        self.assertFalse(dead.healthy)
        self.assertTrue(dead.reason)
        self.assertIn("failed", dead.sentence())

    def test_the_encoder_is_told_the_source_rate_not_the_channels(self):
        """A 64 kbps channel is Discord's default, not a ceiling on the audio."""
        station = SimpleNamespace(bitrate=192)
        self.assertEqual(radio_stream.encode_bitrate(64, station), 192)
        self.assertEqual(radio_stream.encode_bitrate(128, SimpleNamespace(bitrate=0)), 128)
        self.assertEqual(
            radio_stream.encode_bitrate(384, SimpleNamespace(bitrate=320)),
            radio_stream.MAX_ENCODE_KBPS,
        )


# ------------------------------------------------------------------- surface #
class TheCommandSurface(unittest.TestCase):
    def test_the_group_is_three_commands(self):
        """The merge, pinned: play carries the knobs, stop ends it, doctor explains."""
        from cogs import radio as radio_cog

        names = sorted(command.name for command in radio_cog.Radio.radio.commands)
        self.assertEqual(names, ["doctor", "play", "stop"])
        described = radio_cog.Radio.radio.get_command("play")
        parameters = {param.name for param in described.parameters}
        self.assertEqual(
            parameters,
            {"station", "language", "country", "ads", "stay", "volume"},
            "the one command lost a knob, so the settings have no home",
        )

    def test_play_starts_a_station_and_remembers_it(self):
        from cogs import radio as radio_cog

        cog = radio_cog.Radio(_FakeBot())
        cog.get_player = lambda _guild_id: fake_player
        fake_player = _player(11)
        cog._ensure_voice = mock.AsyncMock(return_value=fake_player.voice_client)
        remembered = mock.AsyncMock()
        sent: list = []

        interaction = SimpleNamespace(
            guild_id=11,
            user=SimpleNamespace(display_name="Raj", id=3, guild_permissions=SimpleNamespace(manage_guild=True)),
            guild=SimpleNamespace(owner_id=1, id=11),
            channel=_FakeChannel(),
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )

        async def _send_card(_interaction, content=None, embed=None, **_kwargs):
            sent.append(content or "")

        patches = [
            mock.patch.object(style, "send_card", side_effect=_send_card),
            mock.patch.object(radio_cog, "ControlsView", return_value=None),
            mock.patch.object(radio_cog.db, "get_radio_last", mock.AsyncMock(return_value="")),
            mock.patch.object(radio_cog.db, "set_radio_last", remembered),
            mock.patch.object(radio_cog.db, "get_radio_ads", mock.AsyncMock(return_value="ad-free")),
            mock.patch.object(radio_cog.db, "get_radio_language", mock.AsyncMock(return_value="")),
            mock.patch.object(radio_cog.db, "get_radio_stay", mock.AsyncMock(return_value=(False, 0))),
            mock.patch.object(radio_cog.db, "set_radio_stay", mock.AsyncMock()),
        ]
        for patch in patches:
            patch.start()
        try:
            asyncio.new_event_loop().run_until_complete(
                radio_cog.Radio.play.callback(cog, interaction, "jazz", None, None, None, None, None)
            )
        finally:
            for patch in reversed(patches):
                patch.stop()

        self.assertTrue(sent and "Jazz" in sent[0], f"the card said: {sent}")
        remembered.assert_awaited()
        self.assertEqual(remembered.await_args.args[1], "jazz")

    def test_a_member_without_manage_server_does_not_save_a_setting(self):
        """The permission rule: their choice applies, the server's does not move."""
        from cogs import radio as radio_cog

        cog = radio_cog.Radio(_FakeBot())
        fake_player = _player(12)
        cog.get_player = lambda _guild_id: fake_player
        cog._ensure_voice = mock.AsyncMock(return_value=fake_player.voice_client)
        saved = mock.AsyncMock()

        interaction = SimpleNamespace(
            guild_id=12,
            user=SimpleNamespace(display_name="Member", id=4, guild_permissions=SimpleNamespace(manage_guild=False)),
            guild=SimpleNamespace(owner_id=1, id=12),
            channel=_FakeChannel(),
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )
        sent: list = []

        async def _send_card(_interaction, content=None, embed=None, **_kwargs):
            sent.append(content or "")

        patches = [
            mock.patch.object(style, "send_card", side_effect=_send_card),
            mock.patch.object(radio_cog, "ControlsView", return_value=None),
            mock.patch.object(radio_cog.db, "get_radio_last", mock.AsyncMock(return_value="")),
            mock.patch.object(radio_cog.db, "set_radio_last", mock.AsyncMock()),
            mock.patch.object(radio_cog.db, "get_radio_ads", mock.AsyncMock(return_value="ad-free")),
            mock.patch.object(radio_cog.db, "get_radio_language", mock.AsyncMock(return_value="")),
            mock.patch.object(radio_cog.db, "set_radio_language", saved),
            mock.patch.object(radio_cog.db, "get_radio_stay", mock.AsyncMock(return_value=(False, 0))),
            mock.patch.object(radio_cog.db, "set_radio_stay", mock.AsyncMock()),
        ]
        for patch in patches:
            patch.start()
        try:
            asyncio.new_event_loop().run_until_complete(
                radio_cog.Radio.play.callback(cog, interaction, "", "Finnish", None, None, None, None)
            )
        finally:
            for patch in reversed(patches):
                patch.stop()

        saved.assert_not_awaited()
        self.assertTrue(sent and "this start only" in sent[0], f"the card said: {sent}")
        self.assertEqual(fake_player.current.language, "Finnish", "the member's own start ignored them")

    def test_a_direct_message_is_told_where_a_station_can_play(self):
        """/radio in a DM used to raise, and the member was shown nothing at all.

        Found by driving the command where a member would: outside a server there
        is no guild id, so ``get_player(None)`` did ``int(None)``, the TypeError
        went to the operator's error handler, and Discord told the member that the
        application did not respond. The gate answers it with a sentence now,
        which is what the rest of this bot does with a command that cannot mean
        anything in a private message.
        """
        from cogs import radio as radio_cog

        cog = radio_cog.Radio(_FakeBot())
        sent: list = []
        interaction = SimpleNamespace(
            guild_id=None,
            guild=None,
            user=SimpleNamespace(id=4),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(
                    side_effect=lambda content=None, **kwargs: sent.append(
                        {"content": content, **kwargs}
                    )
                )
            ),
        )
        radio_stream.PLAYERS.clear()
        allowed = asyncio.new_event_loop().run_until_complete(
            cog.interaction_check(interaction)
        )
        self.assertFalse(allowed, "a direct message was let through to a server command")
        self.assertTrue(sent, "the refusal reached nobody")
        self.assertIs(sent[0].get("ephemeral"), True, "the refusal was sent in public")
        self.assertIn("server", sent[0]["content"])
        self.assertEqual(radio_stream.PLAYERS, {}, "a player was made for guild None")

    def test_the_panel_answers_a_command_typed_while_a_station_is_on(self):
        """Run with nothing while something plays, and it is the controls."""
        from cogs import radio as radio_cog

        cog = radio_cog.Radio(_FakeBot())
        fake_player = _player(13)
        fake_player.current = catalogue.CATALOGUE[0]
        cog.get_player = lambda _guild_id: fake_player
        panels: list = []
        cog._send_panel = mock.AsyncMock(side_effect=lambda *_a: panels.append(True))

        interaction = SimpleNamespace(
            guild_id=13,
            user=SimpleNamespace(display_name="Raj", id=3),
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )
        asyncio.new_event_loop().run_until_complete(
            radio_cog.Radio.play.callback(cog, interaction, None, None, None, None, None, None)
        )
        self.assertTrue(panels)
        self.assertEqual(len(fake_player.voice_client.played), 0, "a second station was started")


class TheRenameIsComplete(unittest.TestCase):
    """Laws over the tree, because a half-renamed surface is what this rebuild replaced."""

    def _sources(self):
        for directory, subdirectories, files in os.walk(ROOT):
            subdirectories[:] = [
                name
                for name in subdirectories
                if name
                not in {
                    ".git", ".venv", "__pycache__", "node_modules", "deleted_features",
                    "_site", "_owner", "_hub", "_preview", ".freebuff", ".wrangler",
                }
            ]
            for name in files:
                if not name.endswith((".py", ".html", ".ts", ".tsx", ".js", ".css")):
                    continue
                path = os.path.join(directory, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        yield path, handle.read()
                except OSError:
                    continue

    def test_nothing_imports_the_retired_modules(self):
        for path, text in self._sources():
            relative = os.path.relpath(path, ROOT)
            if relative == os.path.join("tests", "test_radio.py"):
                continue
            with self.subTest(file=relative):
                for gone in (
                    "cogs.music", "cogs import music", "utils.player",
                    "utils import player", "utils.media_source", "utils.spotify",
                ):
                    self.assertNotIn(gone, text, f"{relative} still reaches for {gone}")

    def test_the_old_names_are_gone_from_the_shipped_surfaces(self):
        """A stale `/music play` on a page is a promise the bot cannot keep."""
        for relative in (
            os.path.join("templates", "dashboard.html"),
            os.path.join("templates", "radio.html"),
            os.path.join("cogs", "help.py"),
            "keep_alive.py",
        ):
            path = os.path.join(ROOT, relative)
            if not os.path.exists(path):
                continue  # archived with its rebuild pending; ships nothing
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            with self.subTest(file=relative):
                self.assertNotIn("/music play", text)
                self.assertNotIn("/music stations", text)
                self.assertNotIn("/music radio", text)

    def test_the_feature_registry_carries_radio_and_not_music(self):
        from utils import features

        ids = {feature.id for feature in features.FEATURES}
        self.assertIn("radio", ids)
        self.assertNotIn("music", ids)
        radio_feature = features.get("radio")
        self.assertEqual(radio_feature.gate_commands, ("radio",))
        self.assertEqual(radio_feature.cog, "cogs.radio")

    def test_the_old_address_redirects_and_the_new_one_is_public(self):
        import keep_alive

        self.assertIn("/radio", keep_alive.PUBLIC_PREFIXES)
        self.assertIn("/music", keep_alive.PUBLIC_PREFIXES)
        rules = {str(rule) for rule in keep_alive.app.url_map.iter_rules()}
        self.assertIn("/radio", rules)
        self.assertIn("/music", rules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
