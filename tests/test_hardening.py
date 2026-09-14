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


if __name__ == "__main__":
    unittest.main()
