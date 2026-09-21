"""The cards that are *about* somebody have to reach them.

Three reports, one shape: a card named a person and did not tag them, a soundboard
uploaded a clip that Discord never drew, and a GIF that was never a GIF at all.
All three come from the same layer (``utils.style``), so they are pinned here
through the real command callbacks:

* ``/action``, ``/rizz`` and ``/roast`` pass an ``allowed_mentions`` set naming the
  target, and name nobody else;
* the soundboard hands its card a ``discord.File``, which the card references with
  a File component — an attachment nothing references is an attachment Discord
  silently drops;
* a picture that could not be verified is simply absent, never a broken box.

``cogs.fun`` and ``cogs.social`` import ``send_card`` into their own namespace, so
the tests patch it there, exactly where the command reads it.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import fun as fun_module
from cogs import social as social_module
from tests.support import TempDatabaseMixin
from utils import db, flavor

GUILD = 909
AUTHOR = 111
TARGET = 222


class _FakeAvatar:
    url = "https://example.invalid/avatar.png"


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.bot = False
        self.name = f"User{uid}"
        self.display_name = f"User{uid}"
        self.display_avatar = _FakeAvatar()
        self.mention = f"<@{uid}>"


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = 0

    async def defer(self, **kwargs):
        self.deferred += 1

    def is_done(self):
        return bool(self.messages) or bool(self.deferred)

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeInteraction:
    def __init__(self, author=AUTHOR, guild_id=GUILD):
        self.user = _FakeUser(author)
        me = _FakeUser(999)
        self.guild = SimpleNamespace(id=guild_id, me=me)
        self.guild_id = guild_id
        self.response = _FakeResponse()

    @property
    def sent(self):
        return self.response.messages[-1] if self.response.messages else {}


class _Recorder:
    """Stands in for ``send_card`` and keeps every payload it was handed."""

    def __init__(self):
        self.calls = []

    async def __call__(self, _target, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=1)

    @property
    def last(self):
        return self.calls[-1]

    def mentioned_users(self) -> list[int]:
        allowed = self.last.get("allowed_mentions")
        if allowed is None:
            return []
        return [user.id for user in (getattr(allowed, "users", None) or [])]


class TheActionCommand(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("reach_actions.db")
        await db.setup()
        self.cog = social_module.Social(bot=None)
        self.card = _Recorder()
        self.interaction = _FakeInteraction()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _action(self, action="poke", gif="https://cdn.example.invalid/poke.gif"):
        with mock.patch.object(social_module, "send_card", new=self.card), \
             mock.patch.object(social_module.gifs, "action_gif", new=mock.AsyncMock(return_value=gif)):
            await social_module.Social.action.callback(
                self.cog, self.interaction, SimpleNamespace(value=action), _FakeUser(TARGET)
            )

    async def test_the_target_is_the_only_person_notified(self):
        await self._action()
        self.assertEqual(self.card.mentioned_users(), [TARGET], "the target must be pinged")
        self.assertNotIn(
            AUTHOR, self.card.mentioned_users(), "pinging the author back is noise"
        )
        self.assertIn(f"<@{TARGET}>", self.card.last["embed"].description)

    async def test_the_card_is_deferred_before_the_picture_is_fetched(self):
        """Receiving an interaction twice is what answering after an await looks
        like when the fetch is slow."""
        await self._action()
        self.assertEqual(self.interaction.response.deferred, 1)
        self.assertEqual(self.interaction.response.messages, [])

    async def test_a_verified_picture_is_shown(self):
        gif = "https://cdn.example.invalid/poke.gif"
        await self._action(gif=gif)
        self.assertEqual(self.card.last["embed"].image.url, gif)

    async def test_nothing_verifiable_means_no_picture_rather_than_a_broken_one(self):
        await self._action(gif=None)
        embed = self.card.last["embed"]
        self.assertIsNone(getattr(embed.image, "url", None) or None)
        self.assertTrue(embed.description, "the card still says what happened")

    def test_every_action_has_a_title_colour_and_something_to_say(self):
        """A choice with no entry in ``ACTIONS_DATA`` is an interaction error, not
        a picture problem, so the two lists have to describe the same ten things.
        The choice list is read off the command's own parameters rather than
        duplicated here, which is what makes this a cross-check."""
        offered = {
            option.value
            for param in social_module.Social.action._params.values()
            for option in (param.choices or [])
        }
        self.assertEqual(offered, set(social_module.ACTIONS_DATA))
        for action, data in social_module.ACTIONS_DATA.items():
            self.assertTrue(data.get("title"), action)
            self.assertIsInstance(data.get("color"), int, action)
            self.assertTrue(data.get("lines"), action)
            for line in data["lines"]:
                self.assertIn("{target}", line, f"{action}: {line}")

    async def test_you_cannot_poke_yourself(self):
        with mock.patch.object(social_module, "send_card", new=self.card):
            await social_module.Social.action.callback(
                self.cog, self.interaction, SimpleNamespace(value="poke"), _FakeUser(AUTHOR)
            )
        self.assertEqual(self.card.calls, [])
        self.assertIn("yourself", self.interaction.sent["content"])


class TheFunCommands(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("reach_fun.db")
        await db.setup()
        self.cog = fun_module.Fun(bot=None)
        self.card = _Recorder()
        self.interaction = _FakeInteraction()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _meowsound(self, picture="https://cataas.com/cat/gif"):
        with mock.patch.object(fun_module, "send_card", new=self.card), \
             mock.patch.object(fun_module.gifs, "pet_picture", new=mock.AsyncMock(return_value=picture)):
            await fun_module.Fun.meowsound.callback(self.cog, self.interaction)

    async def test_the_sound_clip_travels_with_the_card(self):
        """The bug: the file was uploaded and Discord never drew it, because a
        Components V2 message shows an attachment only when a component
        references it. The fix is the ``file`` keyword plus a File component,
        which ``utils.style`` builds and ``tests/test_style.py`` pins."""
        await self._meowsound()
        attachment = self.card.last.get("file")
        self.assertIsInstance(attachment, discord.File, "no audio was attached")
        self.assertTrue(
            attachment.filename.endswith((".ogg", ".mp3")), attachment.filename
        )

    async def test_the_soundboard_shows_an_actual_picture(self):
        await self._meowsound(picture="https://cataas.com/cat/gif")
        self.assertEqual(self.card.last["embed"].image.url, "https://cataas.com/cat/gif")

    async def test_the_soundboard_still_plays_when_no_picture_is_available(self):
        await self._meowsound(picture=None)
        self.assertIsInstance(self.card.last.get("file"), discord.File)

    async def test_the_soundboard_speaks_this_servers_flavour(self):
        cat = await self._meowsound()
        self.assertIn("cat", self.card.last["embed"].footer.text.lower())
        await flavor.set_flavor(GUILD, "dog")
        await self._meowsound()
        footer = self.card.last["embed"].footer.text.lower()
        self.assertNotIn("cat", footer, f"a dog server still says cat: {footer}")

    async def test_the_neutral_flavour_looks_for_no_animal_at_all(self):
        await flavor.set_flavor(GUILD, "neutral")
        with mock.patch.object(fun_module, "send_card", new=self.card), \
             mock.patch.object(fun_module.gifs, "pet_picture", new=mock.AsyncMock()) as pet:
            await fun_module.Fun.meowsound.callback(self.cog, self.interaction)
        pet.assert_not_called()
        self.assertIsNone(getattr(self.card.last["embed"].image, "url", None) or None)

    async def test_meow_uses_the_flavour_for_word_and_picture(self):
        await flavor.set_flavor(GUILD, "dog")
        with mock.patch.object(fun_module, "send_card", new=self.card), \
             mock.patch.object(fun_module.gifs, "pet_picture", new=mock.AsyncMock(return_value="https://dog.example.invalid/dog.jpg")) as pet:
            await fun_module.Fun.meow.callback(self.cog, self.interaction)
        pet.assert_awaited()
        self.assertEqual(pet.await_args.args[0], "dog")
        self.assertEqual(self.card.last["embed"].image.url, "https://dog.example.invalid/dog.jpg")

    async def test_rizz_tags_the_person_it_is_aimed_at(self):
        with mock.patch.object(fun_module, "send_card", new=self.card):
            await fun_module.Fun.rizz.callback(self.cog, self.interaction, _FakeUser(TARGET))
        self.assertEqual(self.card.mentioned_users(), [TARGET])

    async def test_roast_tags_the_person_it_is_aimed_at(self):
        with mock.patch.object(fun_module, "send_card", new=self.card):
            await fun_module.Fun.roast.callback(self.cog, self.interaction, _FakeUser(TARGET))
        self.assertEqual(self.card.mentioned_users(), [TARGET])

    async def test_a_ping_is_only_ever_something_a_caller_asked_for(self):
        """Everything else stays silent: a leaderboard that tags ten people is the
        reason the default exists. ``/aura`` names someone and must not ping them
        — it passes no mention set, and ``utils.style`` fills in none."""
        with mock.patch.object(fun_module, "send_card", new=self.card):
            await fun_module.Fun.aura.callback(self.cog, self.interaction, _FakeUser(TARGET))
        self.assertIsNone(self.card.last.get("allowed_mentions"))


class TheFlavorCommand(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("reach_flavor.db")
        await db.setup()
        self.cog = fun_module.Fun(bot=None)
        self.interaction = _FakeInteraction()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_no_argument_lists_the_flavours_and_names_the_current_one(self):
        await fun_module.Fun.flavor_cmd.callback(self.cog, self.interaction, None)
        body = self.interaction.sent["content"]
        self.assertIn("Cat", body)
        for choice in flavor.choices():
            self.assertIn(choice.label, body)

    async def test_a_choice_is_saved_for_this_server(self):
        await fun_module.Fun.flavor_cmd.callback(
            self.cog, self.interaction, SimpleNamespace(value="neutral")
        )
        self.assertEqual((await flavor.get(GUILD)).id, "neutral")

    async def test_it_says_where_the_sound_clips_come_from(self):
        await fun_module.Fun.flavor_cmd.callback(self.cog, self.interaction, None)
        self.assertIn("sound", self.interaction.sent["content"].lower())

    async def test_it_is_a_server_administrator_command(self):
        names = [check.__qualname__ for check in fun_module.Fun.flavor_cmd.checks]
        self.assertTrue(any("has_permissions" in name for name in names), names)

    async def test_a_direct_message_is_told_it_is_per_server(self):
        interaction = _FakeInteraction()
        interaction.guild = None
        interaction.guild_id = None
        await fun_module.Fun.flavor_cmd.callback(self.cog, interaction, None)
        self.assertIn("per-server", interaction.sent["content"].lower())


if __name__ == "__main__":
    unittest.main()
