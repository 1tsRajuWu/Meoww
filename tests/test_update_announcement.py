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
import os
import unittest
from pathlib import Path
from unittest import mock

import bot as bot_module
from utils import config_audit, version

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"
PUSH_COG = Path(__file__).resolve().parent.parent / "cogs" / "bot_config.py"


def _calls(node, name):
    """Every call to ``something.name(...)`` inside ``node``."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == name
    ]


def _named_calls(node, name):
    """Every call to the bare function ``name(...)`` inside ``node``.

    ``_calls`` above matches ``thing.name(...)``; this matches the module-level
    ``name(...)``, which is how the boot starts its own helpers.
    """
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
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

class TestTheBootDoesNotWaitForThePublish(unittest.TestCase):
    """The note a deploy owes must not queue behind the command publish.

    ``on_ready`` used to ``await sync_commands(bot)`` before it started the backup
    loop and posted the release note. Discord paces command writes hard, so a
    server that had lost its list took **eighteen minutes** to be given it back —
    and for those eighteen minutes the live process ran with no backup at all and
    no update note went out, which reads exactly as "the update was never
    deployed". The publish has to start, but the boot must not wait for it.
    """

    def setUp(self):
        self.src = BOT_PY.read_text(errors="replace")
        self.tree = ast.parse(self.src)
        self.on_ready = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_ready"
        )

    def _awaited(self) -> set[str]:
        """The name of every function ``on_ready`` awaits."""
        found = set()
        for node in ast.walk(self.on_ready):
            if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if isinstance(func, ast.Name):
                found.add(func.id)
            elif isinstance(func, ast.Attribute):
                found.add(func.attr)
        return found

    def test_on_ready_does_not_await_the_command_publish(self):
        self.assertNotIn(
            "sync_commands", self._awaited(),
            "on_ready waits for the command publish again: the backup loop and "
            "the release note queue behind minutes of Discord command writes",
        )

    def test_on_ready_still_starts_the_publish(self):
        called = {node.func.id for node in _named_calls(self.on_ready, "_publish_commands_in_background")}
        self.assertTrue(called, "nothing publishes the command tree any more")

    def test_the_publish_is_started_on_the_bot_loop_as_a_task(self):
        helper = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_publish_commands_in_background"
        )
        self.assertTrue(_calls(helper, "create_task"), "the publish is awaited somewhere")
        self.assertTrue(
            _named_calls(helper, "sync_commands"),
            "the background task does not actually publish",
        )
        self.assertTrue(
            _calls(helper, "add_done_callback"),
            "the publish's outcome is never reported",
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
        """An env var name is the actionable part of the note; do not mangle it.

        Both names are ones the bot actually reads, so this cannot quietly rot
        into testing a variable that no longer exists.
        """
        summary = version.summarize_changelog(
            "## Unreleased\n- **Set SUPABASE_SERVICE_KEY and OWNER_USER_ID**: do it now\n"
        )
        self.assertIn("SUPABASE_SERVICE_KEY", summary)
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


class TestSilencingTheAutomaticNotes(unittest.IsolatedAsyncioTestCase):
    """A deploy may keep its release notes to itself.

    ``SILENT_UPDATES=1`` stops the notes that fire without being asked — the
    boot, and the server that has just added the bot — because a bound update
    channel had no way to say no: clearing the binding reads as "not chosen yet",
    so the next boot binds the channel by name again and the notes resume. Only
    the automatic half is silenced. ``/bot push`` is an operator asking for a
    note, which is a different thing from a deploy talking, and it must still
    work — so one test below pins that it was not gated by the same switch.
    """

    def setUp(self):
        self.sent = mock.AsyncMock()
        self.fake_bot = mock.Mock(guilds=[mock.Mock(name="guild")])

    async def _boot(self):
        with mock.patch.object(bot_module, "bot", self.fake_bot), mock.patch.object(
            bot_module, "announce_release_notes_to_guild", self.sent
        ):
            await bot_module._announce_release_notes()

    def _with_switch(self, value):
        """The environment as the host would hand it to the bot.

        The quiet window is pinned wide open here on purpose: this class is about
        the silence switch, and with the clock left alone its answer would depend
        on the hour the suite happens to run, which is the one thing a test about
        a time-of-day feature must not do. ``tests/test_update_window.py`` is
        where the clock itself is driven, by handing it moments.
        """
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["UPDATE_WINDOW_IST"] = "always"
        if value is None:
            os.environ.pop("SILENT_UPDATES", None)
        else:
            os.environ["SILENT_UPDATES"] = value

    async def test_unset_announces_exactly_as_before(self):
        self._with_switch(None)
        await self._boot()
        self.assertEqual(self.sent.await_count, 1)

    async def test_a_silenced_boot_sends_nothing(self):
        self._with_switch("1")
        await self._boot()
        self.sent.assert_not_awaited()

    async def test_another_value_is_not_a_yes(self):
        """Only 1/true/yes/on switch it on, the same set every other flag uses."""
        self._with_switch("maybe")
        await self._boot()
        self.assertEqual(self.sent.await_count, 1)

    def test_a_new_server_is_silenced_by_the_same_switch(self):
        """Both automatic call sites read it, not just the boot.

        Asserted on the source rather than by driving ``on_guild_join``, which
        needs a guild, a join event and the whole channel-binding pass to say
        anything at all; the property under test is that this call site consults
        the same helper, and that is visible in the tree.
        """
        tree = ast.parse(BOT_PY.read_text())
        joins = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "on_guild_join"
        ]
        self.assertEqual(len(joins), 1)
        called = _named_calls(joins[0], "_release_notes_are_silenced")
        self.assertTrue(called, "on_guild_join announces a new server without asking")

    def test_the_on_demand_push_is_not_gated(self):
        """``/bot push`` is a person asking, so the switch may not reach it."""
        source = PUSH_COG.read_text()
        self.assertIn("announce_release_notes_to_guild", source)
        self.assertNotIn("_release_notes_are_silenced", source)

    def test_the_switch_defaults_to_sending(self):
        """Absent means the notes still go out — this is an opt-in silence."""
        self._with_switch(None)
        self.assertFalse(config_audit.flag("SILENT_UPDATES"))

    def test_the_switch_is_declared_as_tuning(self):
        """Declared, and not as a fault: its absence is the working default."""
        setting = next(s for s in config_audit.SETTINGS if s.name == "SILENT_UPDATES")
        self.assertEqual(setting.kind, config_audit.TUNING)
        self.assertFalse(setting.notable)


if __name__ == "__main__":
    unittest.main()
