"""Spotify sharing: link cards, libraries, listen counts and jam links.

No network in these tests: oEmbed is stubbed, and every flow runs against a
throwaway database. What is pinned: link shapes, dedupe, one-listen-per-user,
library aggregation, jam set/replace/remove, and the registry wiring.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord import app_commands

from cogs import music as music_module
from tests.support import TempDatabaseMixin
from utils import db

GUILD = 9307


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})
        self.done = True

    async def edit_message(self, **kwargs):
        self.messages.append({"edit": True, **kwargs})
        self.done = True


class _FakeMember:
    def __init__(self, uid, name=None):
        self.id = uid
        self.display_name = name or f"User{uid}"
        self.mention = f"<@{uid}>"
        self.bot = False


class _FakeGuild:
    def __init__(self, gid=GUILD):
        self.id = gid
        self._members = {}

    def add(self, member):
        self._members[member.id] = member
        return member

    def get_member(self, uid):
        return self._members.get(uid)


def _interaction(user, guild):
    interaction = SimpleNamespace(
        user=user, guild=guild, response=_FakeResponse(), client=SimpleNamespace())
    interaction.guild_id = guild.id
    return interaction


TRACK_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQ"
ALBUM_URL = "https://open.spotify.com/album/1A2GTWGtFfWp7KSQTwWOyo?si=abc123"
DETAILS = {"title": "Test Song", "artist": "Test Artist", "art": "https://i.test/x.jpg"}


async def _track_details_stub(url):
    return dict(DETAILS)


def _rows(table, guild_id=GUILD):
    conn = db.connect()
    try:
        return conn.execute(f"SELECT * FROM {table} WHERE guild_id=?", (guild_id,)).fetchall()
    finally:
        conn.close()


class ParseLinks(unittest.TestCase):
    def test_track_album_playlist_episode(self):
        for url, kind, entity in [
            (TRACK_URL, "track", "4uLU6hMCjMI75M1A2tKUQ"),
            (ALBUM_URL, "album", "1A2GTWGtFfWp7KSQTwWOyo"),
            ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x", "playlist", "37i9dQZF1DXcBWIGoYBM5M"),
            ("https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKy aspect", "episode", "512ojhOuo1ktJprKbVcKy"),
        ]:
            parsed = music_module.parse_spotify_link(url)
            self.assertIsNotNone(parsed, url)
            self.assertEqual(parsed["kind"], kind)
            self.assertEqual(parsed["entity"], entity)
            self.assertTrue(parsed["url"].startswith("https://open.spotify.com/"))

    def test_rejects_non_spotify(self):
        for bad in [None, "", "hello", "https://youtube.com/watch?v=x",
                    "https://open.spotify.com/user/someone"]:
            self.assertIsNone(music_module.parse_spotify_link(bad), bad)


class MusicFlows(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("music.db")
        music_module.DB_PATH = path
        await db.setup()
        self.guild = _FakeGuild()
        self.dj = self.guild.add(_FakeMember(11, "DJ"))
        self.fan = self.guild.add(_FakeMember(22, "FAN"))
        self.cog = music_module.Music(bot=None)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _add(self, user, url):
        interaction = _interaction(user, self.guild)
        with mock.patch.object(music_module, "fetch_track_details", new=_track_details_stub):
            await music_module.Music.music_add.callback(self.cog, interaction, url)
        return interaction

    async def test_add_stores_and_cards(self):
        seen = {}

        async def _capture(target, **kwargs):
            seen.update(kwargs)

        interaction = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "fetch_track_details", new=_track_details_stub):
            with mock.patch.object(music_module.discord.utils, "utcnow", return_value=__import__("datetime").datetime(2026, 1, 1)):
                await music_module.Music.music_add.callback(self.cog, interaction, TRACK_URL)
        rows = _rows("music_tracks")
        self.assertEqual(len(rows), 1)
        sent = interaction.response.messages[-1]
        self.assertIn("embed", sent)
        self.assertIn("Test Song", sent["embed"].title)
        self.assertEqual(sent["embed"].color.value, 0x1DB954)

    async def test_add_rejects_bad_links(self):
        interaction = await self._add(self.dj, "https://youtube.com/watch?v=x")
        self.assertIn("Spotify", interaction.response.messages[-1]["content"])
        self.assertEqual(_rows("music_tracks"), [])

    async def test_add_dedupes(self):
        await self._add(self.dj, TRACK_URL)
        interaction = await self._add(self.fan, TRACK_URL)
        self.assertIn("Already", interaction.response.messages[-1]["content"])
        self.assertEqual(len(_rows("music_tracks")), 1)

    async def test_listen_counts_once_per_user(self):
        await self._add(self.dj, TRACK_URL)

        async def _press(user):
            message = SimpleNamespace(embeds=[SimpleNamespace(url=TRACK_URL)])
            interaction = _interaction(user, self.guild)
            interaction.message = message
            await music_module._listened_callback(interaction)
            return interaction

        first = await _press(self.fan)
        edit = [m for m in first.response.messages if m.get("edit")]
        self.assertTrue(edit, "listen must update the card")
        second = await _press(self.fan)
        self.assertEqual(len(_rows("music_listens")), 1)
        self.assertEqual(_rows("music_listens")[0][2], 22)

    async def test_my_shows_songs_and_listen_counts(self):
        await self._add(self.dj, TRACK_URL)
        await self._add(self.dj, ALBUM_URL)
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO music_listens (guild_id, entity_id, user_id, listened_at)"
                " VALUES (?, ?, ?, ?)",
                (GUILD, "4uLU6hMCjMI75M1A2tKUQ", 22, "now"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO music_listens (guild_id, entity_id, user_id, listened_at)"
                " VALUES (?, ?, ?, ?)",
                (GUILD, "4uLU6hMCjMI75M1A2tKUQ", 33, "now"),
            )
            conn.commit()
        finally:
            conn.close()
        seen = {}

        async def _capture(target, **kwargs):
            seen["embed"] = kwargs.get("embed")

        interaction = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_my.callback(self.cog, interaction, None)
        self.assertIn("2 songs", seen["embed"].title)
        self.assertIn("2 listens", seen["embed"].title)
        self.assertIn("**2**", seen["embed"].description)

    async def test_my_empty_state(self):
        seen = {}

        async def _capture(target, **kwargs):
            seen["embed"] = kwargs.get("embed")

        interaction = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_my.callback(self.cog, interaction, None)
        self.assertIn("haven't added", seen["embed"].description)

    async def test_top_orders_by_listens(self):
        await self._add(self.dj, TRACK_URL)
        await self._add(self.dj, ALBUM_URL)
        conn = db.connect()
        try:
            for uid in (22, 33, 44):
                conn.execute(
                    "INSERT OR IGNORE INTO music_listens (guild_id, entity_id, user_id, listened_at)"
                    " VALUES (?, ?, ?, ?)",
                    (GUILD, "1A2GTWGtFfWp7KSQTwWOyo", uid, "now"),
                )
            conn.execute(
                "INSERT OR IGNORE INTO music_listens (guild_id, entity_id, user_id, listened_at)"
                " VALUES (?, ?, ?, ?)",
                (GUILD, "4uLU6hMCjMI75M1A2tKUQ", 22, "now"),
            )
            conn.commit()
        finally:
            conn.close()
        seen = {}

        async def _capture(target, **kwargs):
            seen["embed"] = kwargs.get("embed")

        interaction = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_top.callback(self.cog, interaction)
        first, second = seen["embed"].description.splitlines()[:2]
        self.assertIn("**3**", first)
        self.assertIn("**1**", second)

    async def test_jam_lifecycle(self):
        seen = {}

        async def _capture(target, **kwargs):
            seen["embed"] = kwargs.get("embed")

        empty = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_jam.callback(self.cog, empty)
        self.assertIn("No active jam", seen["embed"].description)

        setter = _interaction(self.dj, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_jamset.callback(
                self.cog, setter, "https://spotify.link/jamABC123")
        shower = _interaction(self.fan, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_jam.callback(self.cog, shower)
        self.assertIn("join the jam", seen["embed"].description)

        # Replace.
        setter2 = _interaction(self.fan, self.guild)
        with mock.patch.object(music_module, "send_card", new=_capture):
            await music_module.Music.music_jamset.callback(
                self.cog, setter2, "https://open.spotify.com/track/xyz")
        self.assertEqual(len(_rows("music_jam")), 1)

        # Remove.
        remover = _interaction(self.dj, self.guild)
        await music_module.Music.music_jamremove.callback(self.cog, remover)
        self.assertIn("removed", remover.response.messages[-1]["content"].lower())
        self.assertEqual(_rows("music_jam"), [])

        # Removing nothing is gentle.
        remover2 = _interaction(self.dj, self.guild)
        await music_module.Music.music_jamremove.callback(self.cog, remover2)
        self.assertIn("no active jam", remover2.response.messages[-1]["content"].lower())

    async def test_jamset_rejects_non_links(self):
        interaction = _interaction(self.dj, self.guild)
        await music_module.Music.music_jamset.callback(self.cog, interaction, "not a link")
        self.assertIn("Jam link", interaction.response.messages[-1]["content"])
        self.assertEqual(_rows("music_jam"), [])

    async def test_module_gate_refuses_when_off(self):
        bot = SimpleNamespace(is_module_enabled=lambda gid, mod: False)
        cog = music_module.Music(bot=bot)
        interaction = _interaction(self.dj, self.guild)
        self.assertFalse(await cog.interaction_check(interaction))
        self.assertIn("turned off", interaction.response.messages[-1]["content"])

    async def test_cog_error_handler_never_raises(self):
        cog = music_module.Music(bot=None)
        interaction = _interaction(self.dj, self.guild)
        await cog.cog_app_command_error(
            interaction, app_commands.CheckFailure("nope"))
        self.assertIn("permission", interaction.response.messages[-1]["content"].lower())
        quiet = _interaction(self.dj, self.guild)
        await cog.cog_app_command_error(quiet, app_commands.AppCommandError("boom"))
        self.assertEqual(quiet.response.messages, [])


if __name__ == "__main__":
    unittest.main()
