"""`/bot push` — the one command that can put an update live.

A bot process cannot commit, so the command has exactly two honest shapes: with a
Render deploy hook it hands the deploy over and the boot that follows announces the
release by itself; with no hook it says plainly that no code moved and posts the
notes for the version that is actually running. Both are pinned here, along with
the two ways this could quietly lie — posting notes over a deploy that was
attempted, and reporting a deploy that Render refused.

The announcement is driven for real (`bot.announce_release_notes_to_guild`), not
stubbed, so "the notes were posted" means the same code path a boot uses ran.
"""
import unittest
from unittest import mock

import bot as bot_module
from cogs import bot_config
from tests.support import TempDatabaseMixin
from utils import db, deploy

GUILD_ID = 424242
UPDATE_CHANNEL = 5150
TEST_CHANNEL = 5151


def _view_text(view) -> str:
    """Every text body in a Components V2 layout, joined."""
    def walk(node, out):
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, str):
                out.append(content)
            for value in node.values():
                walk(value, out)
        elif isinstance(node, list):
            for value in node:
                walk(value, out)
        return out

    return " ".join(walk(view.to_components(), []))


class _Message:
    def __init__(self, channel):
        self.channel = channel
        self.id = 9000 + len(channel.sent)
        self.texts = []
        self.edits = []

    async def edit(self, **kwargs):
        # Discord refuses an embed on a card, and discord.py refuses a payload
        # carrying both embed and embeds — so a card edit has to be a layout.
        if "embed" in kwargs and "embeds" in kwargs:
            raise TypeError("Cannot mix embed and embeds keyword arguments.")
        if "view" in kwargs:
            self.texts.append(_view_text(kwargs["view"]))
        self.edits.append(kwargs)
        return self


class _Channel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.sent = []
        self.messages = []

    async def send(self, **kwargs):
        message = _Message(self)
        self.sent.append(kwargs)
        self.messages.append(message)
        return message

    # The digest goes out as an embed; the push card as a V2 layout.
    def embed_posts(self):
        return [m for m in self.sent if "embed" in m]

    def cards(self):
        return [m for m in self.sent if "view" in m and not m.get("embeds")]


class _Guild:
    def __init__(self, channels):
        self.id = GUILD_ID
        self.name = "Development"
        self.text_channels = list(channels)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class _User:
    id = 4242
    mention = "<@4242>"


class _Client:
    def __init__(self, owner=True):
        self.owner = owner

    async def is_owner(self, user):
        return self.owner


class _Response:
    def __init__(self):
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self.deferred

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class _Interaction:
    def __init__(self, guild, owner=True):
        self.guild = guild
        self.user = _User()
        self.client = _Client(owner)
        self.response = _Response()
        self.followup = _Followup()

    @property
    def replies(self) -> str:
        return " ".join(str(m) for m in self.followup.messages + self.response.messages)


class PushCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("push.db")
        await db.setup()
        self.update = _Channel(UPDATE_CHANNEL, "update")
        self.test_ground = _Channel(TEST_CHANNEL, "test-ground")
        self.guild = _Guild([self.update, self.test_ground])
        await db.set_setting(GUILD_ID, "update_channel_id", UPDATE_CHANNEL)
        await db.set_setting(GUILD_ID, "test_channel_id", TEST_CHANNEL)
        self.bot = mock.Mock(
            guilds=[self.guild],
            announce_release_notes_to_guild=bot_module.announce_release_notes_to_guild,
        )
        self.cog = bot_config.BotConfig(bot=self.bot)
        self.interaction = _Interaction(self.guild)

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _push(self, *, hook=False, trigger=None, interaction=None, bot=None):
        interaction = interaction or self.interaction
        patched = [mock.patch.object(bot_module, "bot", bot or self.bot)]
        if hook:
            patched.append(mock.patch.object(deploy, "configured", lambda: True))
            patched.append(
                mock.patch.object(
                    deploy, "trigger", trigger or _accepted
                )
            )
        else:
            patched.append(mock.patch.object(deploy, "configured", lambda: False))
        for patch in patched:
            patch.start()
            self.addCleanup(patch.stop)
        await bot_config.BotConfig.push.callback(self.cog, interaction)
        return interaction

    def _card_text(self, channel=None):
        """What the card in the channel says *now*, edits included."""
        channel = channel or self.test_ground
        cards = channel.cards()
        self.assertTrue(cards, "no push card was posted")
        message = channel.messages[-1]
        return message.texts[-1] if message.texts else _view_text(cards[-1]["view"])


async def _accepted():
    return True, "Render accepted the deploy (HTTP 201) and is building the newest commit."


async def _refused():
    return False, "Render refused the deploy (HTTP 401)."


class WithoutAHook(PushCase):
    async def test_the_notes_go_out_and_no_deploy_is_claimed(self):
        await self._push(hook=False)

        posts = self.update.embed_posts()
        self.assertEqual(len(posts), 1, "this version's release notes were not posted")
        self.assertIn("Bot Updated", posts[0]["embed"].title)

        card = self._card_text()
        self.assertIn("nothing deployed", card)
        self.assertIn("RENDER_DEPLOY_HOOK_URL", card, "the way to enable deploys is missing")

    async def test_the_card_reports_in_the_test_channel_not_the_update_one(self):
        await self._push(hook=False)
        self.assertEqual(len(self.test_ground.cards()), 1)
        self.assertEqual(self.update.cards(), [], "the card landed in the public channel")

    async def test_the_card_is_edited_into_its_outcome_not_reposted(self):
        await self._push(hook=False)
        message = self.test_ground.messages[0]
        self.assertTrue(message.edits, "the card was never updated with what happened")
        self.assertEqual(len(self.test_ground.sent), 1, "a second card was posted")
        self.assertIn("nothing deployed", message.texts[-1])

    async def test_a_second_push_has_nothing_new_to_post(self):
        await self._push(hook=False)
        first = self.update.embed_posts()

        self.interaction.followup.messages.clear()
        await self._push(hook=False)

        self.assertEqual(self.update.embed_posts(), first, "the note was posted twice")
        self.assertIn("already been told", self._card_text())

    async def test_a_server_with_nowhere_to_post_still_answers(self):
        await db.set_setting(GUILD_ID, "update_channel_id", None)
        await db.set_setting(GUILD_ID, "test_channel_id", None)

        interaction = await self._push(hook=False)

        self.assertEqual(self.update.sent, [])
        self.assertEqual(self.test_ground.sent, [])
        self.assertIn("no update channel", interaction.replies)
        self.assertIn("nothing deployed", interaction.replies)

    async def test_a_build_without_the_announcement_path_says_so(self):
        class _BareClient:
            """A client from a build that never attached the announcement."""

            guilds = [self.guild]

        self.bot = _BareClient()
        self.cog = bot_config.BotConfig(bot=self.bot)

        interaction = await self._push(hook=False)

        self.assertEqual(self.update.sent, [])
        self.assertIn("failed", self._card_text())


class WithAHook(PushCase):
    async def test_the_deploy_is_triggered_and_no_note_is_posted_by_the_command(self):
        await self._push(hook=True)

        self.assertEqual(
            self.update.embed_posts(),
            [],
            "the boot announces a deploy; posting here would double the note",
        )
        card = self._card_text()
        self.assertIn("Deploy accepted", card)
        self.assertIn("restarts", card, "the operator is not told what happens next")

    async def test_a_refused_deploy_is_reported_rather_than_implied(self):
        await self._push(hook=True, trigger=_refused)

        self.assertEqual(self.update.embed_posts(), [])
        card = self._card_text()
        self.assertIn("Deploy not started", card)
        self.assertIn("401", card)


class OwnerOnly(PushCase):
    async def test_a_member_who_is_not_the_app_owner_cannot_push(self):
        interaction = _Interaction(self.guild, owner=False)
        with mock.patch.dict("os.environ", {"OWNER_USER_ID": ""}):
            triggered = []

            async def _spy():
                triggered.append(True)
                return True, "should not be reached"

            await self._push(hook=True, trigger=_spy, interaction=interaction)

        self.assertEqual(triggered, [], "a non-owner started a deploy")
        self.assertEqual(self.update.sent, [])
        self.assertIn("owner", interaction.replies)


if __name__ == "__main__":
    unittest.main()
