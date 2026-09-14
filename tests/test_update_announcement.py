"""The deploy announcement must be short enough to actually send.

The digest half is pinned below; the other half — that the changelog hash is
written *only* once Discord has accepted the message — is pinned in
``TestAnnouncementOrdering``, because a bare ``except`` around that send is
what made every update note vanish.

Discord rejects an embed whose description is longer than 4,096 characters
with an HTTP 400, and the send used to be wrapped in a bare ``except`` while
the changelog hash was stored regardless — so an oversized or failed
announcement was marked as delivered and never retried. These tests pin the
digest itself: it stays inside the limit, it names the newest entries, and it
drops the markdown that means nothing outside the file.
"""
import ast
import unittest
from pathlib import Path

from utils import version

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


def _calls(node, name):
    """Every call to ``something.name(...)`` inside ``node``."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == name
    ]


def _parent_map(tree):
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


class TestAnnouncementOrdering(unittest.TestCase):
    """The hash records that a note was *delivered*, not that one was owed.

    It used to be written after the post attempt but unconditionally, so a
    missing channel, a rejected embed or a restart mid-announcement consumed
    the note and no later boot ever retried it. The write must stay inside the
    ``try`` that wraps the send.
    """

    def setUp(self):
        self.src = BOT_PY.read_text(errors="replace")
        self.tree = ast.parse(self.src)
        self.parents = _parent_map(self.tree)

    def test_the_hash_is_stored_at_all(self):
        self.assertTrue(_calls(self.tree, "set_last_hash"), "announcement never records")

    def _guarding_try(self, node):
        """The nearest ``try`` above ``node`` whose body contains a send."""
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.Try) and any(
                _calls(stmt, "send") for stmt in node.body
            ):
                return node
        return None

    def test_every_store_sits_inside_the_try_that_sends(self):
        for store in _calls(self.tree, "set_last_hash"):
            self.assertIsNotNone(
                self._guarding_try(store),
                "set_last_hash is reachable when the send fails",
            )

    def test_the_send_happens_before_the_store(self):
        stores = _calls(self.tree, "set_last_hash")
        self.assertTrue(stores)
        for store in stores:
            guard = self._guarding_try(store)
            sends = [n for stmt in guard.body for n in _calls(stmt, "send")]
            self.assertTrue(sends, "the guarded block never sends")
            self.assertLess(
                max(n.lineno for n in sends), store.lineno,
                "the hash is written before the message is sent",
            )

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

    def test_keeps_identifiers_intact(self):
        """An env var name is the actionable part of the note; do not mangle it."""
        summary = version.summarize_changelog(
            "## Unreleased\n- **Set UPLOAD_CHANNEL_ID and OWNER_USER_ID**: do it now\n"
        )
        self.assertIn("UPLOAD_CHANNEL_ID", summary)
        self.assertIn("OWNER_USER_ID", summary)

    def test_still_strips_emphasis(self):
        summary = version.summarize_changelog(
            "## Unreleased\n- **Thing**: this is _important_ and *this* too\n"
        )
        self.assertNotIn("_important_", summary)
        self.assertNotIn("*this*", summary)

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
