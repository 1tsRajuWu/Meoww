"""The deploy announcement must be short enough to actually send.

Discord rejects an embed whose description is longer than 4,096 characters
with an HTTP 400, and the send used to be wrapped in a bare ``except`` while
the changelog hash was stored regardless — so an oversized or failed
announcement was marked as delivered and never retried. These tests pin the
digest itself: it stays inside the limit, it names the newest entries, and it
drops the markdown that means nothing outside the file.
"""
import unittest

from utils import version

SAMPLE = """# Changelog

## Unreleased
- **Uploads really upload now**: `/upload` used to hand back an empty
  file.kiwi folder, so the link contained nothing at all. It now streams the
  bytes to catbox.moe and the message links to the file itself.
- **The update note can't go missing**: the announcement was marked as sent
  even when Discord refused it.
- Plain entry without any bold lead at all, just a sentence. And a second
  sentence that should not be in the digest.

## 2.0.0
- **Older change**: this section is not news any more.
"""


class TestSummaryShape(unittest.TestCase):
    def setUp(self):
        self.summary = version.summarize_changelog(SAMPLE)

    def test_fits_discord_description_limit(self):
        self.assertLess(len(self.summary), 4096)

    def test_shows_newest_section_header(self):
        self.assertIn("Unreleased", self.summary)

    def test_ignores_older_sections(self):
        self.assertNotIn("Older change", self.summary)

    def test_names_the_newest_entry(self):
        self.assertIn("Uploads really upload now", self.summary)

    def test_no_markdown_code_or_links(self):
        summary = version.summarize_changelog(
            "## Unreleased\n- **Thing**: see `utils/db.py` and [docs](https://x.test)\n"
        )
        self.assertNotIn("`", summary)
        self.assertNotIn("](", summary)

    def test_one_line_per_entry(self):
        for line in self.summary.splitlines():
            if line.startswith("• "):
                self.assertLessEqual(len(line), version.ITEM_LIMIT + 4)

    def test_caps_the_number_of_entries(self):
        many = "## Unreleased\n" + "".join(
            f"- **Entry {i}**: detail\n" for i in range(50)
        )
        summary = version.summarize_changelog(many)
        self.assertEqual(summary.count("• "), version.MAX_ITEMS)
        self.assertIn("more in CHANGELOG.md", summary)

    def test_respects_a_small_budget(self):
        summary = version.summarize_changelog(SAMPLE, max_items=50, budget=120)
        self.assertLessEqual(len(summary), 200)

    def test_never_returns_nothing(self):
        self.assertTrue(version.summarize_changelog(""))
        self.assertTrue(version.summarize_changelog("## Unreleased\n- x\n"))

    def test_real_changelog_is_sendable(self):
        """The shipped CHANGELOG must produce a postable digest."""
        text = version.CHANGELOG.read_text(errors="replace")
        summary = version.summarize_changelog(text)
        self.assertLess(len(summary), 4096)
        self.assertIn("•", summary)


if __name__ == "__main__":
    unittest.main()
