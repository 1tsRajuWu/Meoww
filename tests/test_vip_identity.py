"""The VIP commands live in the VIP picker only.

The main bot and the VIP bot run this same tree, but ``/premium`` belongs to
the VIP instance: the main bot leaves it out of every scope it publishes,
which takes it off its pickers and hands the ceiling slots back. Unknown
identity (tests, dry runs, pre-login) keeps everything — only a logged-in
main instance may narrow a scope.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from utils import command_scope, permissions


class IdentityFilter(unittest.TestCase):
    def test_main_leaves_premium_out(self):
        names = command_scope.for_identity(
            {"help", "marry", "premium", "bond", "music"}, is_vip=False)
        self.assertEqual(names, {"help", "marry"})

    def test_vip_keeps_only_vip(self):
        names = command_scope.for_identity(
            {"help", "marry", "premium", "bond", "music"}, is_vip=True)
        self.assertEqual(names, {"premium", "bond", "music"})

    def test_unknown_identity_keeps_everything(self):
        names = command_scope.for_identity(
            {"help", "premium", "bond", "music"}, is_vip=None)
        self.assertEqual(names, {"help", "premium", "bond", "music"})

    def test_app_id_match(self):
        self.assertTrue(permissions.is_vip_app_id(permissions.VIP_BOT_ID))
        self.assertTrue(permissions.is_vip_app_id(str(permissions.VIP_BOT_ID)))
        self.assertFalse(permissions.is_vip_app_id(1548380269776605324))
        self.assertFalse(permissions.is_vip_app_id(None))


class ForceSyncFilter(unittest.TestCase):
    def test_main_token_sync_drops_premium(self):
        import force_sync

        commands = [{"name": "marry"}, {"name": "premium"}, {"name": "bond"}, {"name": "music"}]
        kept = force_sync._identity_commands(commands, 1548380269776605324)
        self.assertEqual([c["name"] for c in kept], ["marry"])

    def test_vip_token_sync_keeps_only_premium(self):
        import force_sync

        commands = [{"name": "marry"}, {"name": "premium"}, {"name": "bond"}, {"name": "music"}]
        kept = force_sync._identity_commands(commands, permissions.VIP_BOT_ID)
        self.assertEqual([c["name"] for c in kept], ["premium", "bond", "music"])


class BootPublisherFilter(unittest.TestCase):
    def _scope(self, user_id, everything):
        import bot as bot_module

        bot = SimpleNamespace(
            user=SimpleNamespace(id=user_id) if user_id is not None else None,
            guilds=[],
        )
        with mock.patch.object(
            bot_module.modules, "get_states", return_value={},
            create=True,
        ):
            guild = SimpleNamespace(id=1, name="Test")
            return bot_module._scope_names(bot, guild, everything)

    def test_main_boot_scope_has_no_premium(self):
        names = self._scope(1548380269776605324, ["marry", "premium", "bond", "music"])
        self.assertNotIn("premium", names)
        self.assertNotIn("bond", names)
        self.assertNotIn("music", names)
        self.assertIn("marry", names)

    def test_vip_boot_scope_keeps_premium(self):
        names = self._scope(permissions.VIP_BOT_ID, ["marry", "premium", "bond", "music"])
        self.assertIn("premium", names)
        self.assertIn("bond", names)
        self.assertIn("music", names)
        self.assertNotIn("marry", names)

    def test_unknown_identity_keeps_premium(self):
        names = self._scope(None, ["marry", "premium", "bond", "music"])
        self.assertIn("premium", names)
        self.assertIn("bond", names)
        self.assertIn("music", names)

    def test_questions_are_vip_only(self):
        self.assertIn("questions", permissions.VIP_ONLY_COMMANDS)
        self.assertIn("question", permissions.VIP_ONLY_COMMANDS)
        main_names = command_scope.for_identity({"questions", "question", "help"}, is_vip=False)
        self.assertEqual(main_names, {"help"})
        vip_names = command_scope.for_identity({"questions", "question", "help"}, is_vip=True)
        self.assertEqual(vip_names, {"questions", "question"})


if __name__ == "__main__":
    unittest.main()
