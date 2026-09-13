"""Shared helpers for tests that touch the database.

These guards exist because a test once pointed at the project's real ``bot.db``:
``cloud_sync`` kept its own copy of the database path, so patching
``utils.db.DB_PATH`` silently left the backup code reading and overwriting the
developer's database. Nothing in this suite may ever touch a file inside the
project, and the two paths must be provably the same object.
"""
import os
import shutil
import tempfile

from utils import cloud_sync, db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def assert_not_project_database(path: str) -> None:
    """Refuse any path that lives inside the project directory."""
    resolved = os.path.abspath(path)
    if resolved == ROOT or resolved.startswith(ROOT + os.sep):
        raise RuntimeError(
            f"refusing to run tests against a file inside the project: {resolved}"
        )


class TempDatabaseMixin:
    """Point the whole application at a throwaway SQLite file."""

    def use_temp_database(self, name: str = "test.db") -> str:
        self._temp_dir = tempfile.mkdtemp(prefix="meow-test-")
        self._original_db_path = db.DB_PATH
        path = os.path.join(self._temp_dir, name)
        db.DB_PATH = path
        assert_not_project_database(path)

        # The backup code must resolve the same file, not a copy of its own.
        resolved = os.path.abspath(cloud_sync.db_path())
        if resolved != os.path.abspath(path):
            db.DB_PATH = self._original_db_path
            raise RuntimeError(
                "cloud_sync does not share utils.db's database path "
                f"({resolved!r} != {os.path.abspath(path)!r})"
            )
        return path

    def release_temp_database(self) -> None:
        db.DB_PATH = self._original_db_path
        cloud_sync._remote_checked = False
        cloud_sync._last_sync_hash = None
        shutil.rmtree(getattr(self, "_temp_dir", ""), ignore_errors=True)
