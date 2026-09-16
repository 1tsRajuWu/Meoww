"""The Terms and the Privacy Policy must be *readable* in Discord.

`/tos` and `/privacy` used to slice the file at Discord's 4,096-character
description limit, so a longer policy lost its last sections silently — and both
documents in this repository are longer than that. These tests render the real
files through the real renderer and assert that every section arrives, that the
embed stays inside Discord's limits, and that the documents still cover the
things a member would look for: the market, the virtual currency and the
Supabase backup.
"""
import os
import unittest

import discord

from cogs import legal
from utils import features

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLegalDocuments(unittest.TestCase):
    def setUp(self):
        self.terms = legal.read_document("TERMS.md")
        self.privacy = legal.read_document("PRIVACY.md")

    def test_both_documents_exist_and_are_not_empty(self):
        self.assertGreater(len(self.terms), 500, "TERMS.md looks empty")
        self.assertGreater(len(self.privacy), 500, "PRIVACY.md looks empty")

    def test_the_documents_are_longer_than_discords_description_limit(self):
        """The reason the section-by-section render exists — keep it honest."""
        self.assertGreater(max(len(self.terms), len(self.privacy)), legal.MAX_DESCRIPTION)

    def test_every_section_reaches_the_card(self):
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            rendered = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
            _, sections = legal.split_document(text)
            self.assertTrue(sections, f"{name} has no ## sections")
            for heading, _ in sections:
                self.assertIn(heading, rendered, f"section {heading!r} was dropped")

    def test_the_last_words_are_on_the_card(self):
        """The old renderer's failure mode: the tail of the file disappeared."""
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            tail = text.strip().split("\n")[-1].strip()
            rendered = "\n".join(
                [embed.description or ""] + [f.value for f in embed.fields]
            )
            self.assertIn(tail, rendered, f"the end of {name} is missing")

    def test_the_embed_is_inside_every_discord_limit(self):
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            self.assertLessEqual(len(embed.description or ""), legal.MAX_DESCRIPTION)
            self.assertLessEqual(len(embed.fields), legal.MAX_FIELDS)
            total = len(embed.description or "")
            for field in embed.fields:
                self.assertLessEqual(len(field.name), legal.MAX_FIELD_NAME)
                self.assertLessEqual(len(field.value), legal.MAX_FIELD_VALUE)
                total += len(field.value)
            self.assertLessEqual(total, legal.MAX_TOTAL_TEXT, f"{name} would be rejected")

    def test_an_oversized_document_points_at_the_repository(self):
        """Nothing is dropped silently: a cut tail says where the rest is."""
        huge = "# Doc\n\n## Section\n\n" + ("word " * 3000)
        embed = legal.document_embed("Doc", huge, "HUGE.md")
        rendered = "\n".join(f.value for f in embed.fields)
        self.assertIn("repository", rendered)

    def test_the_policies_cover_what_the_bot_actually_does(self):
        for needle in ("market", "virtual", "backup", "role"):
            self.assertIn(needle, self.terms.lower(), f"TERMS.md never mentions {needle}")
        for needle in ("market", "sqlite", "supabase", "backup"):
            self.assertIn(needle, self.privacy.lower(), f"PRIVACY.md never mentions {needle}")

    def test_the_third_parties_named_are_the_ones_the_code_uses(self):
        """A policy that lists the wrong companies is worse than none."""
        for host in ("binance", "tradingview", "github", "spotify", "tenor", "reddit"):
            self.assertIn(host, self.privacy.lower(), f"{host} is not disclosed")

    def test_the_commands_are_registered_and_still_gated_as_everyone(self):
        from utils import permissions

        self.assertEqual(permissions.COMMAND_ACCESS["tos"], permissions.EVERYONE)
        self.assertEqual(permissions.COMMAND_ACCESS["privacy"], permissions.EVERYONE)
        self.assertIn("cogs.legal", features.cog_paths())

    def test_a_missing_file_says_so_rather_than_showing_nothing(self):
        self.assertEqual(legal.read_document("NOPE.md"), "")
        embed = legal.document_embed("Privacy Policy", "Document not found.")
        self.assertIn("not found", embed.description)


if __name__ == "__main__":
    unittest.main()
