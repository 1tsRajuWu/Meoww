"""Tests for utils/games.py ad-free game repository and directory."""
import unittest

from utils import games


class TestGamesLookup(unittest.TestCase):
    def test_catalog_entries_have_valid_data(self):
        for key, info in games.CATALOG.items():
            self.assertTrue(info.title, f"{key} has empty title")
            self.assertTrue(info.category, f"{key} has empty category")
            self.assertTrue(info.description, f"{key} has empty description")
            self.assertTrue(info.primary_url.startswith("http"), f"{key} primary_url invalid: {info.primary_url}")
            self.assertTrue(info.primary_label, f"{key} has empty primary_label")
            if info.secondary_url:
                self.assertTrue(info.secondary_url.startswith("http"), f"{key} secondary_url invalid")
                self.assertTrue(info.secondary_label, f"{key} missing secondary_label")

    def test_exact_lookup_matches_catalog(self):
        mc = games.lookup_game("minecraft")
        self.assertIn("Minecraft", mc.title)
        self.assertIn("luanti", mc.primary_url)

        chess = games.lookup_game("chess")
        self.assertIn("Lichess", chess.title)
        self.assertEqual(chess.primary_url, "https://lichess.org/")

    def test_alias_lookup_works(self):
        poke = games.lookup_game("pokerogue")
        self.assertIn("Pokémon", poke.title)

        epic = games.lookup_game("freebies")
        self.assertIn("Epic", epic.title)

    def test_arbitrary_query_fallback_generates_safe_links(self):
        res = games.lookup_game("Elden Ring")
        self.assertIn("Elden Ring", res.title)
        self.assertIn("itch.io", res.primary_url)
        self.assertIn("Elden+Ring", res.primary_url)
        self.assertIn("steampowered.com", res.secondary_url)

    def test_popular_suggestions_returns_choices(self):
        suggestions = games.popular_suggestions("")
        self.assertGreaterEqual(len(suggestions), 5)
        filtered = games.popular_suggestions("mine")
        self.assertTrue(any("Minecraft" in s for s in filtered))


if __name__ == "__main__":
    unittest.main()
