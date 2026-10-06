"""Shared helpers for tests that touch the database, or that sign in.

These guards exist because a test once pointed at the project's real ``bot.db``:
``cloud_sync`` kept its own copy of the database path, so patching
``utils.db.DB_PATH`` silently left the backup code reading and overwriting the
developer's database. Nothing in this suite may ever touch a file inside the
project, and the two paths must be provably the same object.

:func:`agree_to_the_policies` is here for the same reason: the clickwrap is at
*every* door, so a test that signs in and expects a destination has to press an
answer, and writing that walk out per test is how a dozen copies of it end up
disagreeing about which button is which.
"""
import os
import re
import shutil
import tempfile

from utils import cloud_sync, db, guild_directory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def assert_not_project_database(path: str) -> None:
    """Refuse any path that lives inside the project directory."""
    resolved = os.path.abspath(path)
    if resolved == ROOT or resolved.startswith(ROOT + os.sep):
        raise RuntimeError(
            f"refusing to run tests against a file inside the project: {resolved}"
        )


def agree_to_the_policies(client, location: str = "/agree", *, decision: str = "accept", next_url: str = ""):
    """Press one of the two answers on the clickwrap, the way a person does.

    ``location`` is where the door sent the browser — ``/agree?next=%2Fowner`` as
    the redirect returned it. Opened through that address rather than through a
    bare ``/agree``, because the destination travels *in the query string of the
    redirect*: a helper that fetched the bare path would quietly accept the
    default destination and keep passing while the wrap stopped carrying
    anything.

    Returns the POST's own response: a redirect to the carried destination when
    accepted, and the \"you are signed out\" page when declined. Both answers go
    through one helper on purpose — a test that could only press *accept* would
    never notice the day declining stopped working, and the symmetry between the
    two is the property the page exists to have.
    """
    path = location if str(location).startswith("/agree") else "/agree"
    body = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    if match is None:
        raise AssertionError(
            "the agreement page rendered no csrf token — it is either not the "
            "page this test thinks it is, or the token stopped being a form field"
        )
    payload = {"csrf_token": match.group(1), "decision": decision}
    # The destination is read out of the form rather than passed in, because that
    # is what a browser does: the door wrapped where the person was going, the
    # page carries it as a hidden field, and the accept POST follows it. A helper
    # that supplied its own would keep passing while the page stopped carrying
    # the destination at all.
    if not next_url:
        carried = re.search(r'name="next" value="([^"]*)"', body)
        next_url = carried.group(1) if carried else ""
    if next_url:
        payload["next"] = next_url
    return client.post("/agree", data=payload)


def agreed_next_url(location: str) -> str:
    """The destination a door wrapped: ``/agree?next=%2Fowner`` → ``/owner``.

    Asserted rather than string-compared, because the query escaping of the same
    target is not what these tests are about.
    """
    import urllib.parse

    query = urllib.parse.urlsplit(location or "").query
    values = urllib.parse.parse_qs(query).get("next") or [""]
    return values[0]


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

        # The server directory is cached per process while the database is per
        # test, so a row remembered by the previous test is a row about a file
        # that no longer exists — and it would answer `_known_guilds()`, which is
        # what the consoles are allowed to offer an operator. Left in place, one
        # test's server list decides another test's page.
        guild_directory.reset()

        # Per-database sync state must never leak from the previous test.
        cloud_sync._remote_checked = False
        cloud_sync._last_sync_hash = None
        cloud_sync._restore_blocked_reason = None
        cloud_sync._restored_from = None
        cloud_sync._restored_counts = {}
        cloud_sync._cloud_has_data = False
        cloud_sync._shrink_refusals = 0
        cloud_sync._lease_touched = 0.0
        # The backup reading's own memory, for the same reason: a test that
        # restored, uploaded or refused must not decide the next one's answer.
        cloud_sync._restored_at = 0.0
        cloud_sync._last_contact_ts = 0.0
        cloud_sync._last_refusal_reason = None
        cloud_sync._last_upload_ts = 0.0
        cloud_sync._consecutive_upload_failures = 0
        return path

    def release_temp_database(self) -> None:
        db.DB_PATH = self._original_db_path
        cloud_sync._remote_checked = False
        cloud_sync._last_sync_hash = None
        cloud_sync._restore_blocked_reason = None
        cloud_sync._restored_from = None
        cloud_sync._restored_counts = {}
        cloud_sync._cloud_has_data = False
        cloud_sync._shrink_refusals = 0
        cloud_sync._lease_touched = 0.0
        cloud_sync._restored_at = 0.0
        cloud_sync._last_contact_ts = 0.0
        cloud_sync._last_refusal_reason = None
        cloud_sync._last_upload_ts = 0.0
        cloud_sync._consecutive_upload_failures = 0
        shutil.rmtree(getattr(self, "_temp_dir", ""), ignore_errors=True)
