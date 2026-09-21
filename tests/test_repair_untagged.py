"""The one-shot apology script, tested where it can be tested safely.

Pointing this at a live server is the dangerous part, so the parts that decide
*what* it would do are pure functions: the window, which ids a message names, and
the wording. Those are pinned here — including the two ways the message reading
could go wrong, because the cards this script exists for are Components V2
messages, whose text is in components and whose parsed mention list can be empty.
"""
import datetime as dt
import importlib.util
import os
import unittest
from types import SimpleNamespace

import discord

from tests.support import TempDatabaseMixin
from utils import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script():
    """The script as its own module (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "repair_untagged_actions", os.path.join(ROOT, "scripts", "repair_untagged_actions.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = load_script()

GUILD = 5150
BOT_ID = 42


class _Message:
    def __init__(self, *, content="", author_id=BOT_ID, created_at=None, mentions=(), components=None, embeds=()):
        self.content = content
        self.author = SimpleNamespace(id=author_id)
        self.created_at = created_at or dt.datetime(2026, 9, 20, 22, 11, tzinfo=dt.timezone.utc)
        self.mentions = list(mentions)
        self.components = components
        self.embeds = list(embeds)
        self.id = 1


class TheWindow(unittest.TestCase):
    def test_a_local_range_is_turned_into_utc(self):
        start, end = repair.window(
            dt.date(2026, 9, 21), (1.0, 5.0), dt.timedelta(hours=5, minutes=30)
        )
        # 01:00 on the 21st in +05:30 is 19:30 on the 20th in UTC.
        self.assertEqual(start, dt.datetime(2026, 9, 20, 19, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(end, dt.datetime(2026, 9, 20, 23, 30, tzinfo=dt.timezone.utc))

    def test_the_window_is_half_open_and_aware(self):
        start, end = repair.window(dt.date(2026, 9, 21), (1.0, 5.0), dt.timedelta(0))
        self.assertTrue(repair.in_window(start, start, end), "the start is inside")
        self.assertFalse(repair.in_window(end, start, end), "the end is not")
        just_before = end - dt.timedelta(seconds=1)
        self.assertTrue(repair.in_window(just_before, start, end))

    def test_a_naive_timestamp_is_read_as_utc_rather_than_refused(self):
        start, end = repair.window(dt.date(2026, 9, 21), (1.0, 5.0), dt.timedelta(0))
        naive = start.replace(tzinfo=None) + dt.timedelta(minutes=5)
        self.assertTrue(repair.in_window(naive, start, end))

    def test_the_parsers_refuse_nonsense(self):
        import argparse

        self.assertEqual(repair.parse_hours("1-5"), (1.0, 5.0))
        self.assertEqual(repair.parse_hours("0-24"), (0.0, 24.0))
        for junk in ("5", "5-1", "-1-3", "a-b", "0-25"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=junk):
                repair.parse_hours(junk)
        self.assertEqual(repair.parse_tz("+05:30"), dt.timedelta(hours=5, minutes=30))
        self.assertEqual(repair.parse_tz("-04:00"), -dt.timedelta(hours=4))
        # A missing sign means east of UTC, which is what the default offset is.
        self.assertEqual(repair.parse_tz("5:30"), dt.timedelta(hours=5, minutes=30))
        for junk in ("+3:75", "+15:00", "nope", "", "utc"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=junk):
                repair.parse_tz(junk)


class WhatAMessageNames(unittest.TestCase):
    def test_a_legacy_embed_is_read(self):
        embed = SimpleNamespace(title="👉 Boop / Poke!", description="<@111> poked <@222>")
        message = _Message(embeds=[embed])
        self.assertEqual(repair.mentioned_ids(message), [111, 222])

    def test_a_v2_card_is_read_even_when_the_parsed_mention_list_is_empty(self):
        """This is the shape that caused the whole problem: a Components V2 card
        carries its text in components, and ``Message.mentions`` can come back
        empty even though two people are named inside it."""
        components = [
            {
                "type": 17,
                "components": [
                    {"type": 10, "content": "**Bonk!** <@111> got bonked by <@222> 💫"},
                ],
            }
        ]
        message = _Message(content="", mentions=[], components=components)
        self.assertEqual(repair.mentioned_ids(message), [111, 222])

    def test_a_parsed_component_object_is_read_too(self):
        """discord.py hands a received card a list of component *objects* rather
        than the JSON, and the mention lives inside a TextDisplay's ``content``.
        Reading only one of the two shapes found nothing in a channel full of
        these cards."""
        text = discord.ui.TextDisplay(content="**Bonk!** <@111> got bonked by <@222> 💫")
        thumb = discord.ui.Thumbnail("https://example.invalid/x.png")
        section = discord.ui.Section(text, accessory=thumb)
        container = discord.ui.Container(section)
        message = _Message(content="", mentions=[], components=[container])
        self.assertEqual(repair.mentioned_ids(message), [111, 222])
        self.assertIn("Bonk!", repair.message_text(message))

    def test_the_parsed_list_and_the_raw_text_are_merged_without_duplicates(self):
        message = _Message(
            content="<@111> and <@111>",
            mentions=[SimpleNamespace(id=111), SimpleNamespace(id=222)],
        )
        self.assertEqual(repair.mentioned_ids(message), [111, 222])

    def test_the_bot_itself_is_never_tagged_back(self):
        message = _Message(content=f"<@{BOT_ID}> poked <@222>")
        self.assertEqual(repair.mentioned_ids(message, exclude={BOT_ID}), [222])

    def test_a_message_naming_nobody_is_not_a_card(self):
        message = _Message(content="just a bot log line")
        self.assertEqual(repair.mentioned_ids(message), [])
        self.assertFalse(repair._looks_like_a_card(message, BOT_ID))

    def test_only_the_bots_own_messages_are_candidates(self):
        human = _Message(content="<@222> hi", author_id=999)
        self.assertFalse(repair._looks_like_a_card(human, BOT_ID))
        self.assertTrue(repair._looks_like_a_card(_Message(content="<@222>!"), BOT_ID))


class TheNote(unittest.TestCase):
    def test_it_tags_the_people_and_quotes_what_was_said(self):
        body = repair.note_text(["<@222>"], "<@111> poked <@222>'s cheek")
        self.assertTrue(body.startswith("<@222> "), body)
        self.assertIn("sorry", body.lower())
        self.assertIn("<@111> poked <@222>'s cheek", body)

    def test_it_says_what_went_wrong_without_blaming_anybody(self):
        body = repair.note_text(["<@222>"], "line").lower()
        self.assertIn("never actually tagged you", body)
        self.assertIn("our fault", body)

    def test_a_long_excerpt_is_trimmed(self):
        body = repair.note_text(["<@222>"], "x" * 5_000)
        self.assertLess(len(body), 600, "the note must stay readable in a reply")

    def test_a_message_with_no_readable_text_still_gets_a_note(self):
        body = repair.note_text(["<@222>"], "")
        self.assertIn("<@222>", body)
        self.assertNotIn("\n> ", body, "nothing to quote, so no quote")


class TheMarker(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """Idempotency, because the alternative is apologising twice."""

    async def asyncSetUp(self):
        self.use_temp_database("repair_marker.db")
        await db.setup()

    async def asyncTearDown(self):
        self.release_temp_database()

    async def test_nothing_answered_yet(self):
        self.assertEqual(await repair._already_replied(GUILD), set())

    async def test_answered_messages_are_remembered_per_server(self):
        await repair._remember(GUILD, {1, 2, 3})
        self.assertEqual(await repair._already_replied(GUILD), {1, 2, 3})
        self.assertEqual(await repair._already_replied(GUILD + 1), set())

    async def test_a_hand_edited_marker_does_not_crash_the_run(self):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, repair.MARKER_KEY, "12 34, 56"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(await repair._already_replied(GUILD), {12, 34, 56})


if __name__ == "__main__":
    unittest.main()
