"""`/add`: the words it offers, and the channel a person meant by a name.

The command takes a **name** rather than a channel picker, which is a decision
with a consequence: four ways of naming one channel have to resolve to the same
channel, or a server whose rooms are decorated — ``💬・bot-cmd`` is how a server
spells ``bot-cmd`` — is told the channel does not exist while it is looking at it.
The resolver is pure, so all of that is pinned here rather than through a live
gateway.

The second half is the vocabulary. A role in :data:`bot_channels.ROLES` with no
words is a picker entry reading ``cmdlog``, and the drift is invisible until
somebody reads the menu — so a missing label is a failing test instead.
"""
import unittest
from types import SimpleNamespace

from utils import bot_channels


def _channel(channel_id, name):
    return SimpleNamespace(id=channel_id, name=name)


CHANNELS = [
    _channel(1, "general"),
    _channel(2, "💬・bot-cmd"),
    _channel(3, "mod-logs"),
]


class TestTheWords(unittest.TestCase):
    def test_every_role_has_words_a_person_can_read(self):
        for role in bot_channels.ROLES:
            with self.subTest(role=role):
                self.assertIn(role, bot_channels.LABELS)
                self.assertIn(role, bot_channels.SHORT)
                self.assertTrue(bot_channels.label(role).strip())
                self.assertTrue(bot_channels.short(role).strip())

    def test_no_label_is_invented_for_a_role_that_does_not_exist(self):
        """The other direction: a label left behind by a retired role is drift."""
        self.assertEqual(sorted(set(bot_channels.LABELS) - set(bot_channels.ROLES)), [])
        self.assertEqual(sorted(set(bot_channels.SHORT) - set(bot_channels.ROLES)), [])

    def test_an_unknown_role_is_shown_as_itself(self):
        self.assertEqual(bot_channels.label("whatever"), "whatever")


class TestTheNameAPersonTyped(unittest.TestCase):
    def test_an_exact_name_finds_its_channel(self):
        self.assertIs(bot_channels.find_channel(CHANNELS, "general"), CHANNELS[0])

    def test_a_decorated_name_finds_the_channel_it_decorates(self):
        """The same rule the boot binder uses, so the two cannot disagree."""
        self.assertIs(bot_channels.find_channel(CHANNELS, "bot-cmd"), CHANNELS[1])
        self.assertIs(bot_channels.find_channel(CHANNELS, "💬・bot-cmd"), CHANNELS[1])

    def test_a_mention_finds_its_channel(self):
        self.assertIs(bot_channels.find_channel(CHANNELS, "<#3>"), CHANNELS[2])

    def test_a_bare_id_finds_its_channel(self):
        self.assertIs(bot_channels.find_channel(CHANNELS, "2"), CHANNELS[1])

    def test_a_number_that_matches_nothing_is_not_a_name_to_fall_back_on(self):
        """A wrong id is a wrong channel; it is not a channel called ``99``."""
        self.assertIsNone(bot_channels.find_channel(CHANNELS, "99"))

    def test_nothing_and_nonsense_find_nothing(self):
        for wanted in ("", "   ", None, "not-a-channel"):
            with self.subTest(wanted=wanted):
                self.assertIsNone(bot_channels.find_channel(CHANNELS, wanted))

    def test_the_match_ignores_case(self):
        self.assertIs(bot_channels.find_channel(CHANNELS, "GENERAL"), CHANNELS[0])

    def test_a_name_two_channels_share_answers_with_one_of_them(self):
        """Discord permits it, so refusing is the only alternative — and the card
        names the channel it bound, so the choice is visible rather than silent."""
        twice = [_channel(1, "general"), _channel(2, "general")]
        self.assertIn(bot_channels.find_channel(twice, "general"), twice)


if __name__ == "__main__":
    unittest.main()
