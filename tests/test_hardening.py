"""Regression tests for the audit fixes that are not money arithmetic.

Each one pins a concrete defect found in a full pass over the live tree: a
published default dashboard credential, moderation case numbers that disagreed
with the stored record, deferred interactions left unanswered, a listener killed
by an unguarded delete, two read-modify-write races, a starboard row that could
never recover, and commands crashing in DMs.
"""
import asyncio
import unittest
from pathlib import Path

from utils import db, features

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class NoPublishedCredential(unittest.TestCase):
    """A hardcoded passkey is a credential in a public repository."""

    def test_the_old_default_is_gone_everywhere(self):
        for rel in ("keep_alive.py", "README.md", "HANDOFF.md", ".env.example", "CHANGELOG.md"):
            path = ROOT / rel
            if path.exists():
                self.assertNotIn("Him@2009@Meoww", path.read_text(encoding="utf-8"), rel)

    def test_passkey_has_no_fallback_value(self):
        source = _read("keep_alive.py")
        # The environment value is used verbatim; there is no `or "<something>"`
        # fallback, so an unset variable yields an empty (unusable) passkey.
        self.assertIn('ADMIN_PASSKEY = (os.environ.get("ADMIN_PASSKEY") or "").strip()', source)

    def test_passkey_endpoints_reject_an_empty_credential(self):
        source = _read("keep_alive.py")
        # Passkey APIs go through _passkey_ok, which refuses an empty/missing
        # ADMIN_PASSKEY and never reads the secret from the query string.
        self.assertIn("def _passkey_ok", source)
        self.assertIn("if not ADMIN_PASSKEY:", source)
        self.assertGreaterEqual(source.count("_passkey_ok()"), 3)
        self.assertNotIn('request.args.get("passkey")', source)


class ModCaseNumbers(unittest.TestCase):
    """The number shown to users must be the row's real id."""

    def test_case_number_is_the_inserted_row_id(self):
        import os
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="meow-case-")
        original = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "cases.db")
            conn = db.connect()
            conn.execute("""CREATE TABLE mod_cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, case_type TEXT,
                user_id INTEGER, moderator_id INTEGER, reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            conn.commit()
            conn.close()

            first = asyncio.run(db.add_mod_case(1, "Warn", 10, 99, "first"))
            second = asyncio.run(db.add_mod_case(1, "Kick", 11, 99, "second"))

            self.assertEqual(first, 1)
            self.assertEqual(second, 2)

            # Deleting a row must not renumber later cases — the old COUNT(*)+1
            # counter would have handed out a number that already existed.
            conn = db.connect()
            conn.execute("DELETE FROM mod_cases WHERE case_id = ?", (first,))
            conn.commit()
            conn.close()

            third = asyncio.run(db.add_mod_case(1, "Ban", 12, 99, "third"))
            self.assertEqual(third, 3)

            conn = db.connect()
            stored = {row[0] for row in conn.execute("SELECT case_id FROM mod_cases")}
            conn.close()
            self.assertIn(third, stored, "the reported case number must exist in the table")
        finally:
            db.DB_PATH = original
            shutil.rmtree(tmp, ignore_errors=True)


class DeferredErrorsAreAnswered(unittest.TestCase):
    def test_error_handler_falls_back_to_a_followup(self):
        source = _read("bot.py")
        self.assertIn("if interaction.response.is_done():", source)
        self.assertIn("await interaction.followup.send(message, ephemeral=True)", source)


class ListenerHardening(unittest.TestCase):
    def test_automod_delete_is_guarded(self):
        source = _read("cogs/automod.py")
        self.assertIn("except (discord.Forbidden, discord.NotFound, discord.HTTPException)", source)

    def test_vc_reward_write_is_atomic(self):
        source = _read("cogs/leveling.py")
        # The per-minute payout is passed in as arguments, so the signature grew
        # with the reward maths; what matters is that the read-decide-write still
        # runs inside one ``db.run_atomic`` transaction.
        self.assertIn("def _vc_tick(cur, xp_gain=", source)
        self.assertIn("await asyncio.to_thread(db.run_atomic, _vc_tick)", source)

    def test_chat_cooldown_map_is_pruned(self):
        source = _read("cogs/leveling.py")
        self.assertIn("if len(self._chat_cooldowns) > 4096:", source)

    def test_starboard_recovers_when_its_card_was_deleted(self):
        source = _read("cogs/starboard.py")
        self.assertIn("except discord.NotFound:", source)
        self.assertIn("INSERT OR REPLACE INTO starboard_posts", source)


class DmGuard(unittest.TestCase):
    def test_dm_allow_list_only_names_real_commands(self):
        """A typo would silently make a command unusable in DMs."""
        import re

        commands = set()
        for path in list((ROOT / "cogs").glob("*.py")) + [ROOT / "bot.py"]:
            source = path.read_text(encoding="utf-8")
            commands.update(re.findall(r'@app_commands\.command\(name="([^"]+)"', source))
            commands.update(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', source))
        unknown = sorted(features.DM_ALLOWED_COMMANDS - commands)
        self.assertEqual(unknown, [], f"DM allow-list names non-existent commands: {unknown}")

    def test_bot_enforces_the_dm_guard(self):
        source = _read("bot.py")
        self.assertIn("features.DM_ALLOWED_COMMANDS", source)
        self.assertIn("only works inside a server", source)

    def test_member_cache_helper_defaults_to_false(self):
        from utils import style

        self.assertFalse(style.member_cache_complete(object()))


class GrowingTablesAreIndexed(unittest.TestCase):
    """A missing index is invisible until the table is big, and then it is the query.

    The composite primary keys cover economy, user_levels, item_effects, loans and
    market_holdings, but the chart series, the trade history, /inventory, /quote and
    the warning list all filter on columns that had no index at all.
    """

    def test_the_access_paths_have_indexes(self):
        import os
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="meow-idx-")
        original = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "indexes.db")
            asyncio.run(db.setup())
            conn = db.connect()
            try:
                names = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            finally:
                conn.close()
        finally:
            db.DB_PATH = original
            shutil.rmtree(tmp, ignore_errors=True)

        for index in (
            "idx_market_history_symbol_ts",
            "idx_market_trades_owner",
            "idx_inventory_owner",
            "idx_custom_quotes_guild",
            "idx_warnings_owner",
        ):
            self.assertIn(index, names, f"{index} is missing, so its query scans")


class TheAuditFixesAreInTheShippedPages(unittest.TestCase):
    def test_the_market_page_announces_its_state(self):
        """Its status line is the only place "trading paused" is ever said."""
        html = _read("templates/market_live.html")
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('aria-pressed=', html)
        self.assertIn('role="img"', html)
        self.assertIn(':focus-visible', html, "keyboard users need to see where they are")
        self.assertNotIn("tr.innerHTML = `", html, "data is text, never markup")

    def test_the_composer_keeps_a_draft_across_an_expired_session(self):
        html = _read("templates/dashboard.html")
        self.assertIn("sessionStorage.setItem(DRAFT_KEY", html)
        self.assertIn("dropDraft();", html, "a sent or cleared composer drops it")
        self.assertGreaterEqual(
            html.count("saveDraft();"), 3,
            "every request helper has to save before it redirects to sign in",
        )


class ThePagesStayReadable(unittest.TestCase):
    """Small text on these dark cards must clear the 4.5:1 AA floor.

    Two colours were below it — ``#64748b``, which carried 22 pieces of small
    text (counters, notes, timestamps) at 3.7:1, and the accent colour used *as
    text* at 3.9:1 while it is fine behind white on a filled button. Neither is
    visible as a defect to whoever reintroduces one, so the ratio is checked.
    """

    #: Every surface these pages paint text on, lightest last (the worst case).
    SURFACES = ("#090c10", "#131821", "#0e131b")
    PAGES = ("templates/dashboard.html", "templates/market_live.html", "templates/login.html")
    FLOOR = 4.5

    @staticmethod
    def _channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    @classmethod
    def _luminance(cls, color: str) -> float:
        raw = color.lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * cls._channel(r) + 0.7152 * cls._channel(g) + 0.0722 * cls._channel(b)

    @classmethod
    def _ratio(cls, fg: str, bg: str) -> float:
        a, b = cls._luminance(fg), cls._luminance(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    @classmethod
    def _unreadable_text(cls, source: str) -> list[tuple[str, float]]:
        """Text colours in a template that fail the floor on every surface.

        Rules that declare their own ``background`` are skipped: white on a
        filled accent button is measured against that fill, not the page.
        """
        import re

        style = "\n".join(re.findall(r"<style>(.*?)</style>", source, re.S))
        root = re.search(r":root\s*\{(.*?)\}", style, re.S)
        variables = dict(
            re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", root.group(1) if root else "")
        )
        bad = []
        for rule in re.findall(r"[^{}]+\{[^{}]*\}", style):
            if "background" in rule:
                continue
            for value in re.findall(r"(?<![-\w])color:\s*(var\(--[\w-]+\)|#[0-9a-fA-F]{3,6})", rule):
                color = variables.get(value[4:-1]) if value.startswith("var(") else value
                if not color or color.lower() in ("#090c10", "#090c11"):
                    continue  # unresolvable, or the page colour itself
                worst = min(cls._ratio(color, surface) for surface in cls.SURFACES)
                if worst < cls.FLOOR:
                    bad.append((color, round(worst, 2)))
        return bad

    def test_no_page_paints_text_below_the_floor(self):
        for page in self.PAGES:
            self.assertEqual(
                self._unreadable_text(_read(page)), [],
                f"{page} has text that fails the 4.5:1 contrast floor",
            )

    def test_the_check_catches_a_colour_that_fails(self):
        """A guard that cannot fail is decoration: feed it the two that did."""
        regressed = "<style>:root { --text-muted: #64748b; --accent: #5865F2; }" \
                    " p { color: var(--text-muted); } a { color: var(--accent); }</style>"
        found = dict((color, ratio) for color, ratio in self._unreadable_text(regressed))
        self.assertIn("#64748b", found)
        self.assertIn("#5865F2", found)

    def test_white_on_a_filled_button_is_measured_against_its_fill(self):
        filled = "<style>:root { --accent: #5865F2; }" \
                 " .btn { background: var(--accent); color: #fff; }</style>"
        self.assertEqual(self._unreadable_text(filled), [])


if __name__ == "__main__":
    unittest.main()
