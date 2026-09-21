"""One server's taste, and the fact that it is only that server's.

The setting exists because the bot's whole voice was written for a cat and a
server that does not want cats had no way to say so without switching the Fun
module off entirely. So the things worth pinning are: unset means exactly what
every server already had, a choice never leaks into the next guild, and a value
that is not a flavour is refused rather than guessed at — a typo must not silently
change how every card in somebody's server looks.
"""
import unittest
from types import SimpleNamespace

from tests.support import TempDatabaseMixin
from utils import db, flavor

GUILD = 4242


class ReadingAndWriting(unittest.TestCase):
    def test_every_flavour_is_describable(self):
        for choice in flavor.choices():
            self.assertIn(choice.emoji, choice.describe())
            self.assertIn(choice.label, choice.describe())
            self.assertTrue(choice.summary)

    def test_cat_is_the_default_and_it_is_the_one_that_always_existed(self):
        self.assertEqual(flavor.DEFAULT, "cat")
        self.assertEqual(flavor.get_cached(GUILD, None).id, "cat")
        self.assertEqual(flavor.get_cached(GUILD, "nonsense").id, "cat")
        self.assertEqual(flavor.get_cached(GUILD, "").id, "cat")

    def test_the_cat_flavour_is_the_cat_voice(self):
        cat = flavor.FLAVORS["cat"]
        self.assertIn("Meow.", cat.sounds)
        self.assertEqual(cat.pet, "cat")

    def test_names_people_actually_type_are_understood(self):
        self.assertEqual(flavor.parse("Cat"), "cat")
        self.assertEqual(flavor.parse("  DOG "), "dog")
        self.assertEqual(flavor.parse("🐶 Dog"), "dog")
        self.assertEqual(flavor.parse("neutral"), "neutral")
        # "I don't want the cat stuff" is the request this setting answers.
        for word in ("none", "plain", "off", "no"):
            self.assertEqual(flavor.parse(word), "neutral", word)

    def test_junk_is_refused_rather_than_guessed(self):
        for junk in ("catdog", "", "  ", "kitty", 7, object()):
            self.assertIsNone(flavor.parse(junk), repr(junk))
        self.assertIsNone(flavor.parse(None))

    def test_a_flavour_object_round_trips(self):
        self.assertEqual(flavor.parse(flavor.FLAVORS["dog"]), "dog")


class ThePerServerSwitch(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("flavor.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_a_server_that_never_chose_gets_cat(self):
        self.assertEqual((await flavor.get(GUILD)).id, "cat")

    async def test_a_choice_is_saved_and_read_back(self):
        chosen = await flavor.set_flavor(GUILD, "dog")
        self.assertEqual(chosen.id, "dog")
        self.assertEqual((await flavor.get(GUILD)).id, "dog")

    async def test_one_servers_choice_is_not_anothers(self):
        await flavor.set_flavor(GUILD, "neutral")
        self.assertEqual((await flavor.get(GUILD)).id, "neutral")
        self.assertEqual((await flavor.get(GUILD + 1)).id, "cat")

    async def test_an_unknown_choice_changes_nothing(self):
        self.assertIsNone(await flavor.set_flavor(GUILD, "banana"))
        self.assertEqual((await flavor.get(GUILD)).id, "cat")

    async def test_no_guild_means_no_setting_and_the_default_read(self):
        self.assertIsNone(await flavor.set_flavor(None, "dog"))
        self.assertIsNone(await flavor.set_flavor(0, "dog"))
        self.assertEqual((await flavor.get(0)).id, "cat")
        self.assertEqual((await flavor.get(None)).id, "cat")

    async def test_an_unreadable_row_still_answers_with_a_flavour(self):
        """A restored backup, a hand-edited row, a raw pre-JSON string — the
        commands read this on their hot path, so it can never raise."""
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, flavor.SETTING_KEY, "not json at all"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual((await flavor.get(GUILD)).id, "cat")


class WhatTheCommandsRead(unittest.TestCase):
    def test_the_flavour_carries_the_things_the_cards_ask_for(self):
        for choice in flavor.choices():
            self.assertTrue(choice.soundboard_title)
            self.assertTrue(choice.audio_credit)
            self.assertIsInstance(choice.sounds, tuple)
            if choice.pet is not None:
                self.assertIn(choice.pet, ("cat", "dog"))

    def test_the_audio_credit_does_not_pretend_to_be_a_dog(self):
        """The bundled clips are cat recordings whatever the flavour is. The
        honest label is the whole reason this field exists."""
        self.assertIn("cat", flavor.FLAVORS["cat"].audio_credit.lower())
        for other in ("dog", "neutral"):
            self.assertNotIn("cat", flavor.FLAVORS[other].audio_credit.lower())


if __name__ == "__main__":
    unittest.main()
