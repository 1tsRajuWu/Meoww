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
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO guild_modules (guild_id, module_name, enabled) VALUES (?, ?, ?)",
                (111, "music", 0),
            )
            conn.commit()
        finally:
            conn.close()
        modules.invalidate(111)
        states = modules.get_states(111)
        self.assertNotIn("music", states)
        self.assertNotIn("welcome", states)

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
