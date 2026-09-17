"""Guild defaults set by /updatedb, and the quote card's footer.

``/updatedb`` used to write the home server's channel IDs into whichever guild
ran it. In any other server that pointed ``log_channel_id`` at a channel that does
not exist, so ``_send_log`` silently dropped every moderation action, and it
overwrote whatever the admins had configured.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import bot_config as config_module
from cogs import fun as fun_module
from tests.support import TempDatabaseMixin
from utils import db

FOREIGN_GUILD = 5555
HOME_GUILD = config_module.HOME_GUILD_ID


def _setting(guild_id: int, key: str):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key)
        ).fetchone()
        # db.set_setting stores JSON, so decode to compare against real ints.
        return None if row is None else json.loads(row[0])
    finally:
        conn.close()


class _Channel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _Response:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self.messages.append({"deferred": True})


class _Guild:
    def __init__(self, gid, text_channels):
        self.id = gid
        self.name = f"Guild{gid}"
        self.text_channels = text_channels
        self.owner_id = 1

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class _Interaction:
    def __init__(self, guild):
        self.guild = guild
        self.user = SimpleNamespace(id=1, display_name="Admin", display_avatar=SimpleNamespace(url="https://x.invalid/a.png"))
        self.response = _Response()
        self.followup = _Followup()
        self.client = None


class UpdatedbDefaults(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # bot_config imports utils.db lazily, so patching utils.db is enough.
        self.use_temp_database("defaults.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def _run(self, guild):
        cog = config_module.BotConfig(bot=None)
        interaction = _Interaction(guild)
        await config_module.BotConfig.updatedb.callback(cog, interaction)
        return interaction

    async def test_updatedb_binds_the_house_channels_it_can_see(self):
        """The only way to re-run name-based binding without a restart."""
        guild = _Guild(FOREIGN_GUILD, [_Channel(9101, "update"), _Channel(9102, "bug")])

        interaction = await self._run(guild)

        self.assertEqual(_setting(FOREIGN_GUILD, "update_channel_id"), 9101)
        self.assertEqual(_setting(FOREIGN_GUILD, "bug_channel_id"), 9102)
        notes = " ".join(str(m["content"]) for m in interaction.followup.messages)
        self.assertIn("update=#update", notes)
        self.assertIn("bug=#bug", notes)
        # error, commands and log have no channel here, and saying so is the point:
        # a role silently going nowhere is what this whole layer exists to end.
        self.assertIn("Still unbound", notes)

    async def test_updatedb_leaves_channels_that_are_already_set_alone(self):
        guild = _Guild(FOREIGN_GUILD, [_Channel(9103, "update")])
        await db.set_setting(FOREIGN_GUILD, "update_channel_id", 999)

        await self._run(guild)

        self.assertEqual(_setting(FOREIGN_GUILD, "update_channel_id"), 999)

    async def test_a_foreign_guild_never_receives_the_home_channel_ids(self):
        guild = _Guild(FOREIGN_GUILD, [_Channel(9001, "logs"), _Channel(9002, "bot-cmd")])

        interaction = await self._run(guild)

        self.assertEqual(_setting(FOREIGN_GUILD, "log_channel_id"), 9001)
        self.assertEqual(_setting(FOREIGN_GUILD, "bot_cmd_channel_id"), 9002)
        self.assertNotEqual(_setting(FOREIGN_GUILD, "log_channel_id"), config_module.HOME_LOG_CHANNEL_ID)
        self.assertIn("logs", interaction.followup.messages[-1]["content"])

    async def test_a_foreign_guild_without_those_channels_is_left_alone(self):
        guild = _Guild(FOREIGN_GUILD, [_Channel(9003, "general")])

        interaction = await self._run(guild)

        self.assertIsNone(_setting(FOREIGN_GUILD, "log_channel_id"))
        self.assertIsNone(_setting(FOREIGN_GUILD, "bot_cmd_channel_id"))
        self.assertIn("No `#logs` channel", interaction.followup.messages[-1]["content"])

    async def test_an_existing_setting_is_not_replaced_by_a_foreign_id(self):
        await db.set_setting(FOREIGN_GUILD, "log_channel_id", 4321)
        guild = _Guild(FOREIGN_GUILD, [_Channel(9003, "general")])

        await self._run(guild)

        self.assertEqual(_setting(FOREIGN_GUILD, "log_channel_id"), 4321)

    async def test_the_home_guild_still_gets_its_own_ids(self):
        guild = _Guild(HOME_GUILD, [])

        await self._run(guild)

        self.assertEqual(_setting(HOME_GUILD, "log_channel_id"), config_module.HOME_LOG_CHANNEL_ID)
        self.assertEqual(
            _setting(HOME_GUILD, "bot_cmd_channel_id"), config_module.HOME_BOT_CMD_CHANNEL_ID
        )


class QuoteCardShowsItsLabels(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.use_temp_database("quote.db")
        fun_module.DB_PATH = path
        await db.setup()
        self.sent = []

        async def fake_send_card(_target, **kwargs):
            self.sent.append(kwargs)

        self._patch = mock.patch.object(fun_module, "send_card", new=fake_send_card)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        self.release_temp_database()

    async def _run(self, category="all", mood=None):
        cog = fun_module.Fun(bot=None)
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(display_avatar=SimpleNamespace(url="https://x.invalid/b.png")),
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=1, display_name="Raj", display_avatar=SimpleNamespace(url="https://x.invalid/a.png")),
            response=_Response(),
            channel=SimpleNamespace(),
        )
        choice = SimpleNamespace(value=category) if category else None
        await fun_module.Fun.quote.callback(cog, interaction, choice, mood)
        return interaction

    async def test_the_card_names_its_category(self):
        await self._run(category="all")

        self.assertTrue(self.sent, "no card was sent")
        footer = self.sent[-1]["embed"].footer.text
        self.assertIn("🏷️", footer, "the computed category label was dropped")
        self.assertIn("Raj", footer)

    async def test_the_card_names_its_mood_when_one_is_chosen(self):
        await self._run(category="all", mood="hyped")

        footer = self.sent[-1]["embed"].footer.text
        self.assertIn("🎭", footer)

    async def test_the_footer_never_contains_a_mention(self):
        await self._run(category="all", mood="sad")

        footer = self.sent[-1]["embed"].footer.text
        self.assertNotIn("<@", footer)


if __name__ == "__main__":
    unittest.main()
