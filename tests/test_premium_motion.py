"""Animated VIP cards belong to the VIP instance only.

The gold motion set (crown author, gem thumbnail, sparkle footer, crown
banner) must never render from the main bot: same words, plain card. These
tests pin both sides through the same helper the three commands use.
"""
import unittest
from types import SimpleNamespace

from cogs import premium as premium_module
from utils.style import VIP_BOT_ID

MAIN_ID = 1548380269776605324


def _interaction(bot_user_id):
    return SimpleNamespace(
        client=SimpleNamespace(user=SimpleNamespace(id=bot_user_id)))


class VipOnlyMotion(unittest.TestCase):
    def test_vip_instance_gets_gold_and_motion(self):
        interaction = _interaction(VIP_BOT_ID)
        embed = premium_module._brand_embed(
            interaction, title="T", description="D",
            fields=[("F", "V", True)], thumbnail=True, banner=True)
        self.assertEqual(embed.color.value, 0xFFD700)
        self.assertTrue((embed.author.name or "").startswith("DaCat Premium"))
        self.assertTrue(embed.thumbnail.url.endswith(".gif"))
        self.assertTrue(embed.footer.icon_url.endswith(".gif"))
        self.assertTrue(embed.image.url.endswith(".gif"))

    def test_main_instance_gets_plain_words(self):
        interaction = _interaction(MAIN_ID)
        embed = premium_module._brand_embed(
            interaction, title="T", description="D",
            fields=[("F", "V", True)], thumbnail=True, banner=True)
        self.assertNotEqual(embed.color.value, 0xFFD700)
        author = embed.author.name if embed.author else ""
        self.assertNotIn("👑", author)
        self.assertFalse(embed.thumbnail.url or "")
        self.assertFalse((embed.image.url if embed.image else "") or "")
        self.assertFalse((embed.footer.icon_url if embed.footer else "") or "")

    def test_unknown_instance_is_plain(self):
        interaction = SimpleNamespace(client=SimpleNamespace())
        embed = premium_module._brand_embed(interaction, title="T", description="D")
        self.assertNotEqual(embed.color.value, 0xFFD700)


if __name__ == "__main__":
    unittest.main()
