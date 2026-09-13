"""Music & Radio: the playback-state bugs, tested without a Discord connection.

Three genuine defects lived in the restored cog:

* applying a filter (or a volume change) called ``voice_client.stop()`` and then
  started the rebuilt source by hand, so the stop's after-callback *also* advanced
  the queue — switching a filter threw away a queued song;
* ``/radio`` did the same thing explicitly, so two advances ran: the second found
  an empty queue and cleared ``current`` while the stream was still playing, after
  which ``/nowplaying``, ``/queue`` and ``/skip`` all denied anything was on;
* ``/volume`` only assigned a field, which the live source never read (PCM) or had
  already baked into its ffmpeg command line (opus), so it did nothing at all.

The fix is a generation counter: every source gets an after-callback tagged with
the generation it was created in, and a callback only advances the queue when its
generation is still current. These tests pin that, plus the source-selection and
the no-mention rules.

ffmpeg/yt-dlp are never invoked here: ``_build_source`` is exercised with the
source classes mocked, and ``play_next`` is replaced with a recorder.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import music as music_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 321

Track = music_module.Track


def _track(title: str) -> "Track":
    return Track(title=title, source_url="http://example.invalid/stream", duration=180, requester="Raj")


class _FakeChannel:
    def __init__(self, cid: int = 99):
        self.id = cid
        self.name = "voice"
        self.bitrate = 128000
        self.members = []
        self.guild = SimpleNamespace(id=GUILD)
        self.mention = f"<#{cid}>"


class _FakeVoiceClient:
    """Mimics the parts of VoiceClient/Guild that the player touches.

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


class _FakeBot:
    def __init__(self):
        self.loop = asyncio.get_running_loop()


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeInteraction:
    def __init__(self):
        self.guild = SimpleNamespace(id=GUILD, name="Guild")
        self.user = SimpleNamespace(id=7, display_name="Raj", mention="<@7>")
        self.channel = SimpleNamespace(id=55, mention="<#55>")
        self.response = _FakeResponse()
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class _MusicCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("music.db")
        music_module.DB = db
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    def _player(self):
        bot = _FakeBot()
        player = music_module.GuildMusicPlayer(bot, GUILD)
        vc = _FakeVoiceClient()
        player.voice_client = vc
        return player, vc

    def _recorder(self, player):
        """Replace play_next with something that records and mimics the pop."""
        calls = []

        async def fake_play_next(error=None):
            calls.append(error)
            if player.queue:
                player.current = player.queue.pop(0)

        player.play_next = fake_play_next
        return calls


class FilterRestartKeepsTheQueue(_MusicCase):
    async def test_restarting_for_a_filter_does_not_advance_the_queue(self):
        player, vc = self._player()
        player.current = _track("Now Playing")
        player.queue = [_track("Queued Song")]
        vc.play(object(), after=player._after_for(player._generation))   # a track is playing
        calls = self._recorder(player)

        self.assertTrue(player.restart_current())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(calls, [], "the deliberate stop was treated as a finished song")
        self.assertEqual(len(player.queue), 1, "a queued track was consumed by the restart")
        self.assertEqual(player.current.title, "Now Playing")
        self.assertEqual(vc.stop_calls, 1)

    async def test_the_replacement_source_still_advances_when_it_really_ends(self):
        player, vc = self._player()
        player.current = _track("Now Playing")
        player.queue = [_track("Queued Song")]
        vc.play(object(), after=player._after_for(player._generation))
        calls = self._recorder(player)

        player.restart_current()
        await asyncio.sleep(0)
        self.assertEqual(calls, [])

        vc.after(None)          # the re-rendered track finishes for real
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(len(calls), 1, "the queue stalled after a restart")
        self.assertEqual(player.current.title, "Queued Song")

    async def test_a_stale_callback_from_a_replaced_source_is_ignored(self):
        player, vc = self._player()
        player.current = _track("Now Playing")
        stale = player._after_for(player._generation)
        vc.play(object(), after=stale)
        player.restart_current()          # bumps the generation
        calls = self._recorder(player)

        stale(None)                       # the abandoned source reports it ended
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(calls, [], "an abandoned source advanced the queue")

    async def test_restart_is_refused_when_nothing_is_playing(self):
        player, vc = self._player()
        player.current = _track("Nothing playing")
        self.assertFalse(player.restart_current())
        self.assertEqual(vc.stop_calls, 0)

    async def test_restart_is_refused_without_a_stream_url(self):
        player, vc = self._player()
        player.current = Track(title="Spotify pending", source_url="", duration=1, requester="Raj")
        vc._playing = True
        self.assertFalse(player.restart_current())


class SourceSelection(_MusicCase):
    async def test_without_libopus_the_volume_is_baked_into_the_ffmpeg_command(self):
        player, _vc = self._player()
        player.volume = 0.5
        with mock.patch.object(music_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(music_module.discord, "FFmpegOpusAudio") as opus_cls:
            source = player._build_source("http://example.invalid/a")

        self.assertIs(source, opus_cls.return_value)
        options = opus_cls.call_args.kwargs["options"]
        self.assertIn("volume=0.5", options, "volume would never reach the stream")

    async def test_with_libopus_the_source_is_wrapped_in_a_live_volume_transformer(self):
        player, _vc = self._player()
        player.volume = 0.25
        with mock.patch.object(music_module.discord.opus, "is_loaded", return_value=True), \
             mock.patch.object(music_module.discord, "FFmpegPCMAudio"), \
             mock.patch.object(music_module.discord, "PCMVolumeTransformer") as vol_cls:
            source = player._build_source("http://example.invalid/a")

        self.assertIs(source, vol_cls.return_value)
        self.assertEqual(vol_cls.call_args.kwargs["volume"], 0.25)

    async def test_the_filter_command_line_is_applied(self):
        player, _vc = self._player()
        player.current_filter = "nightcore"
        with mock.patch.object(music_module.discord.opus, "is_loaded", return_value=False), \
             mock.patch.object(music_module.discord, "FFmpegOpusAudio") as opus_cls:
            player._build_source("http://example.invalid/a")

        options = opus_cls.call_args.kwargs["options"]
        self.assertIn(music_module.AUDIO_FILTERS["nightcore"]["ffmpeg"], options)


class RadioStartsExactlyOneStream(_MusicCase):
    async def _run_radio(self, playing: bool):
        player, vc = self._player()
        vc._playing = playing
        if playing:
            vc.play(object(), after=player._after_for(player._generation))

        cog = music_module.Music(bot=SimpleNamespace(loop=asyncio.get_running_loop()))
        cog.get_player = lambda _gid: player

        async def fake_ensure_voice(_interaction):
            return vc

        cog._ensure_voice = fake_ensure_voice
        calls = self._recorder(player)
        interaction = _FakeInteraction()

        await music_module.Music.radio.callback(cog, interaction, SimpleNamespace(value="lofi"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return player, calls, interaction

    async def test_replacing_a_playing_track_advances_only_once(self):
        player, calls, interaction = await self._run_radio(playing=True)

        self.assertEqual(len(calls), 1, "two advances ran; the second cleared `current`")
        self.assertIsNotNone(player.current, "current was cleared while the radio plays")
        self.assertEqual(player.current.title, music_module.RADIO_STATIONS["lofi"]["name"])
        self.assertEqual(player.current.requester, "Raj")
        self.assertTrue(interaction.edits, "the radio card was never sent")

    async def test_starting_from_silence_advances_once(self):
        player, calls, _interaction = await self._run_radio(playing=False)

        self.assertEqual(len(calls), 1)
        self.assertEqual(player.current.title, music_module.RADIO_STATIONS["lofi"]["name"])


class MusicCardsDoNotPing(unittest.TestCase):
    def test_no_user_mentions_in_music_output(self):
        with open("cogs/music.py", encoding="utf-8") as handle:
            source = handle.read()

        # Channel mentions are fine (they never notify); user mentions are not.
        self.assertNotIn("interaction.user.mention", source)
        self.assertNotIn("member.mention", source)
        self.assertIn("interaction.user.display_name", source)


class FormatDuration(unittest.TestCase):
    def test_live_streams_read_as_live(self):
        self.assertEqual(music_module.format_duration(0), "Live Stream 🔴")
        self.assertEqual(music_module.format_duration(-5), "Live Stream 🔴")

    def test_minutes_and_seconds(self):
        self.assertEqual(music_module.format_duration(65), "01:05")

    def test_hours(self):
        self.assertEqual(music_module.format_duration(3725), "01:02:05")


if __name__ == "__main__":
    unittest.main()
