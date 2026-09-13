"""The feature registry exists to stop one feature being described in six
places. These tests are what make that real: if any surface drifts out of sync
with ``utils/features.py``, they fail."""
import re
import unittest
from pathlib import Path

from utils import features

ROOT = Path(__file__).resolve().parent.parent


class TestRegistryShape(unittest.TestCase):
    def test_validate_reports_no_problems(self):
        self.assertEqual(features.validate(), [])

    def test_ids_and_help_keys_are_unique(self):
        ids = [f.id for f in features.FEATURES]
        self.assertEqual(len(ids), len(set(ids)))
        help_keys = [f.help_key for f in features.FEATURES if f.help_key]
        self.assertEqual(len(help_keys), len(set(help_keys)))

    def test_every_gate_targets_a_real_feature(self):
        for command, ids in features.MODULE_COMMAND_MAP.items():
            for feature_id in ids:
                self.assertIn(feature_id, features.FEATURE_IDS, f"{command} -> {feature_id}")

    def test_command_gates_have_no_duplicates(self):
        seen = {}
        for command, ids in features.MODULE_COMMAND_MAP.items():
            self.assertNotIn(command, seen)
            seen[command] = ids

    def test_dashboard_modules_exclude_non_toggleable(self):
        ids = {m["id"] for m in features.dashboard_modules()}
        self.assertEqual(ids, set(features.TOGGLEABLE_IDS))
        self.assertNotIn("system", ids)

    def test_every_cog_path_exists(self):
        for cog in features.cog_paths():
            self.assertTrue(
                (ROOT / (cog.replace(".", "/") + ".py")).exists(),
                f"feature registry references missing cog {cog}",
            )

    def test_cog_paths_are_unique(self):
        paths = features.cog_paths()
        self.assertEqual(len(paths), len(set(paths)))


class TestNoDrift(unittest.TestCase):
    """Cross-check the registry against the places that used to keep copies."""

    def test_help_menu_matches_registry(self):
        from cogs.help import HELP_MODULES

        self.assertEqual(set(HELP_MODULES), set(features.HELP_KEYS))

    def test_bot_has_no_hardcoded_command_map(self):
        source = (ROOT / "bot.py").read_text()
        self.assertNotIn("MODULE_COMMAND_MAP = {", source)
        self.assertIn("features.MODULE_COMMAND_MAP", source)

    def test_keep_alive_has_no_hardcoded_module_list(self):
        source = (ROOT / "keep_alive.py").read_text()
        self.assertNotIn('"name": "Leveling & XP"', source)
        self.assertIn("features.dashboard_modules()", source)

    def test_cog_module_ids_are_registered(self):
        """Every literal module id a cog checks must exist in the registry.

        A typo here silently disables a feature, which is exactly the class of
        bug the registry was introduced to prevent.
        """
        pattern = re.compile(r"is_module_enabled\(\s*[^,]+,\s*[\"']([a-z0-9_-]+)[\"']")
        found = {}
        for cog in sorted((ROOT / "cogs").glob("*.py")):
            for match in pattern.finditer(cog.read_text()):
                found.setdefault(match.group(1), []).append(cog.name)
        self.assertTrue(found, "expected at least one module check in the cogs")
        for module_id, files in found.items():
            self.assertIn(
                module_id, features.FEATURE_IDS,
                f"cog(s) {files} check unknown module id '{module_id}'",
            )

    def test_default_state_is_shared(self):
        from utils import db

        self.assertEqual(set(features.DEFAULT_DISABLED_MODULES), db.DEFAULT_DISABLED_MODULES)
        for feature in features.FEATURES:
            if feature.toggleable:
                self.assertEqual(
                    db.module_default_enabled(feature.id), feature.enabled_by_default
                )


if __name__ == "__main__":
    unittest.main()
