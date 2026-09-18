"""No test, and no preview, may write the operator's live database.

Both halves of this were true by accident before they were tested:

* ``test_scheduler.py`` built its schema and inserted rows through the *default*
  database path, so 3,680 fixtures (guild ``777001``) landed in the real
  ``bot.db`` — which the running bot uploaded as the production backup.
* ``scripts/preview_dashboard.py`` served pages that write through the same
  module, so a click in the preview changed live state: every module was turned
  off in the Development server during a playthrough, and commands there started
  answering "the X system is currently turned off".

``tests/__init__.py`` and the harness now both point somewhere else. These tests
are what fails if that ever stops being true: the live file is addressed only to
prove nobody touched it, and the harness is checked in its own process, because
importing it here would move *this* process's database.
"""

import os
import re
import subprocess
import sys
import unittest

from tests.support import assert_not_project_database
from utils import cloud_sync, db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DB = os.path.join(ROOT, "bot.db")


def _fingerprint(path: str):
    """``(size, mtime_ns)`` — enough to notice any write, cheap enough to run."""
    if not os.path.exists(path):
        return None
    stat = os.stat(path)
    return (stat.st_size, stat.st_mtime_ns)


class SuiteDatabaseIsolation(unittest.TestCase):
    """The suite runs on a file of its own, in a temporary directory."""

    def test_the_suite_database_is_not_inside_the_project(self):
        assert_not_project_database(db.DB_PATH)  # raises; the assertion is the check
        self.assertNotEqual(
            os.path.abspath(db.DB_PATH),
            os.path.abspath(LIVE_DB),
            "the suite is pointed at the operator's own database",
        )

    def test_the_cloud_code_reads_the_same_file_the_bot_does(self):
        """A backup of a *different* file is how the suite's data got uploaded."""
        self.assertEqual(
            os.path.abspath(cloud_sync.db_path()),
            os.path.abspath(db.DB_PATH),
            "cloud_sync resolved a database path of its own",
        )

    def test_the_unittest_runner_gets_the_guard_too(self):
        """CI discovers with ``unittest``, which never loads a ``conftest``.

        The guard therefore lives in ``tests/__init__.py``. This runs one module
the way ``unittest.defaultTestLoader`` does — importing the ``tests`` package
first — and asks what database that process ended up on.
        """
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import unittest\n"
                "unittest.defaultTestLoader.loadTestsFromName('tests.test_bot_channels')\n"
                "from utils import db\n"
                "print('DB_PATH=' + str(db.DB_PATH))\n",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        match = re.search(r"^DB_PATH=(.+)$", proc.stdout, re.M)
        self.assertIsNotNone(match, f"no database reported:\n{proc.stdout[-2000:]}")
        assert_not_project_database(match.group(1).strip())

    def test_the_preview_harness_uses_a_copy(self):
        before = _fingerprint(LIVE_DB)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import scripts.preview_dashboard  # noqa: F401\n"
                "from utils import db\n"
                "print('DB_PATH=' + str(db.DB_PATH))\n",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        match = re.search(r"^DB_PATH=(.+)$", proc.stdout, re.M)
        self.assertIsNotNone(match, f"the harness never reported its database:\n{proc.stdout[-2000:]}")
        harness_db = match.group(1).strip()
        assert_not_project_database(harness_db)
        self.assertEqual(
            _fingerprint(LIVE_DB),
            before,
            "starting the preview harness wrote to the project's bot.db",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
