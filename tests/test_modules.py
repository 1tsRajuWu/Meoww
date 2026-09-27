"""Module state is the thing that used to "turn itself back on".

Two caches and two write paths meant the dashboard and the bot could disagree,
and a failed read silently fell back to enabled. These tests pin the single
authority in ``utils.modules``."""
import asyncio
import os
import threading
import unittest

from tests.support import TempDatabaseMixin
from utils import db, features, modules


class ModuleStateCase(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        modules.invalidate()

    def tearDown(self):
        modules.invalidate()
        self.release_temp_database()

    def test_defaults_are_all_toggleable_features(self):
        states = modules.get_states(111)
        self.assertEqual(set(states), set(features.TOGGLEABLE_IDS))
        for feature_id, enabled in states.items():
            self.assertEqual(enabled, features.default_enabled(feature_id))

    def test_toggle_round_trip(self):
        self.assertTrue(modules.is_enabled(111, "economy"))
        self.assertTrue(modules.set_enabled(111, "economy", False))
        self.assertFalse(modules.is_enabled(111, "economy"))
        self.assertTrue(modules.is_enabled(222, "economy"), "guilds must be isolated")

    def test_toggle_survives_a_restart(self):
        self.assertTrue(modules.set_enabled(111, "fun", False))
        # Simulate a process restart: drop every cache and reload from the DB.
        modules.invalidate()
        self.assertFalse(modules.is_enabled(111, "fun"))
        modules.invalidate()
        self.assertFalse(modules.get_states(111)["fun"])

    def test_failed_defaults_do_not_resurrect(self):
        """A guild with no saved rows must use the feature default, not 'True'."""
        for feature in features.FEATURES:
            if feature.toggleable:
                self.assertEqual(
                    modules.is_enabled(999, feature.id), feature.enabled_by_default
                )

    def test_retired_module_rows_are_ignored(self):
        """A row for a module that is not a feature any more must not come back.

        The example used to be ``music``, which is why this test changed: music
        was archived and its rows outlived it, then rebuilt as ``radio`` — so the
        rows a *retired* module left are named with the ones that really are still
        retired (the archive in ``deleted_features/``). The assertion is about the
        registry being the only source of module ids, which is what stops a switch
        reappearing for something the bot no longer has.
        """
        conn = db.connect()
        try:
            for retired in ("tempvoice", "welcome", "giveaways"):
                conn.execute(
                    "INSERT OR REPLACE INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
                    (111, retired, 0),
                )
            conn.commit()
        finally:
            conn.close()
        modules.invalidate(111)
        states = modules.get_states(111)
        for retired in ("tempvoice", "welcome", "giveaways"):
            self.assertNotIn(retired, states)
        self.assertIn("legacy", states, "the legacy systems are a live feature again")

    def test_the_migration_leaves_no_row_for_a_retired_module(self):
        """A retired module's row is deleted, not merely ignored.

        The test above is about the *reader*: `get_states` drops an unknown id, so
        a stale row cannot resurrect a switch. It does not stop the row from
        existing — and a row for a module the registry no longer declares is
        unreadable by every reader, unwritable by `set_enabled` and invisible in
        the dashboard, which leaves a migration as the only thing that can ever
        remove it. Automod & Shield was merged into Moderation & Logs, so every
        guild that had the shield on was holding exactly that row.

        Run the way a restored database runs the migrations: delete the marker,
        then `setup()` again. The sweep has to leave the live modules alone, which
        is the second half of the assertion — a cleanup that also deleted a
        module the registry declares would be worse than the row it was cleaning.
        """
        conn = db.connect()
        try:
            for module_name in ("giveaway", "market"):
                conn.execute(
                    "INSERT OR REPLACE INTO guild_modules (guild_id, module_name, enabled) "
                    "VALUES (?, ?, ?)",
                    (111, module_name, 1),
                )
            conn.execute(
                "DELETE FROM guild_settings WHERE guild_id = 0 "
                "AND key = 'modules_retired_swept_v1'"
            )
            conn.commit()
        finally:
            conn.close()

        asyncio.run(db.setup())

        conn = db.connect()
        try:
            saved = {
                name
                for (name,) in conn.execute(
                    "SELECT module_name FROM guild_modules WHERE guild_id = 111"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn("giveaway", saved, "a retired module's row outlived the migration")
        self.assertIn("market", saved, "the sweep deleted a module the registry declares")

    def test_unknown_module_is_not_gated_but_cannot_be_written(self):
        self.assertTrue(modules.is_enabled(111, "not_a_real_module"))
        self.assertFalse(modules.set_enabled(111, "not_a_real_module", False))

    def test_core_feature_is_not_toggleable(self):
        self.assertTrue(modules.is_enabled(111, "system"))
        self.assertFalse(modules.set_enabled(111, "system", False))

    def test_set_many_reports_failures(self):
        failed = modules.set_many(111, {"economy": False, "fun": False, "bogus": True})
        self.assertEqual(failed, ["bogus"])
        self.assertFalse(modules.is_enabled(111, "economy"))
        self.assertFalse(modules.is_enabled(111, "fun"))

    def test_missing_database_does_not_start_disabled_modules(self):
        """A read failure must fall back to defaults, never to 'enabled'."""
        db.DB_PATH = os.path.join(self._temp_dir, "does-not-exist.db")
        modules.invalidate()
        with self.assertRaises(Exception):
            db.read_guild_modules_sync(111)
        states = modules.get_states(111)
        self.assertEqual(set(states), set(features.TOGGLEABLE_IDS))

    def test_concurrent_writes_all_persist(self):
        targets = sorted(features.TOGGLEABLE_IDS)
        errors = []

        def worker(module_id):
            try:
                for _ in range(5):
                    if not modules.set_enabled(111, module_id, False):
                        errors.append(f"{module_id} write failed")
                        return
            except Exception as exc:  # pragma: no cover
                errors.append(f"{module_id}: {exc}")

        threads = [threading.Thread(target=worker, args=(m,)) for m in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        modules.invalidate(111)
        states = modules.get_states(111)
        for module_id in targets:
            self.assertFalse(states[module_id], f"{module_id} lost its toggle")

    def test_concurrent_readers_never_see_a_lie(self):
        modules.set_enabled(111, "economy", False)
        results = []

        def reader():
            for _ in range(50):
                results.append(modules.is_enabled(111, "economy"))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(results)
        self.assertTrue(all(value is False for value in results))


if __name__ == "__main__":
    unittest.main()
