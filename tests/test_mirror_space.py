"""The Space mirror, and the four things it must never do.

The machine is the one host in this product that carries no deployment stamp, so
there is no address that can be asked *which commit are you serving* — what
protects it is what this script will and will not send. Four properties, each one
a way a mirror can be quietly wrong: it sends a file no commit holds, it sends
the local database, it offers a binary the Space stores itself on every run, or
it deletes something on the Space that this repository never tracked.

``tests/test_ship.py`` holds the other half of this — that the Space is not a
project ``ship.py`` uploads, and that a machine which does not answer is reported
with the two ways it is kept current.
"""
import unittest
from pathlib import Path
from unittest import mock

import scripts.mirror_space as mirror

ROOT = Path(__file__).resolve().parents[1]


class TheIndexOfFilesIsTheIndex(unittest.TestCase):
    """What leaves is what ``git`` tracks, because everything else is local state."""

    def test_the_list_is_the_index_rather_than_a_walk_of_the_directory(self):
        files = mirror.tracked()
        self.assertIn("keep_alive.py", files)
        self.assertIn("utils/features.py", files)
        self.assertTrue(all(len(sha) == 40 for sha in files.values()), "not git blob ids")
        for untracked in ("bot.db", ".env", "bot.db-wal"):
            with self.subTest(path=untracked):
                self.assertNotIn(
                    untracked,
                    files,
                    "the mirror would send local state to the machine that runs the bot",
                )
        for built in ("_site", "_owner", "_dash", "_market"):
            with self.subTest(built=built):
                self.assertFalse(
                    [p for p in files if p.startswith(built + "/")],
                    f"the mirror would send the {built} build directory",
                )


class NothingIsSentThatTheCommitDoesNotHold(unittest.TestCase):
    """A pushed tree has no hash on it, so the tree has to be the hash's."""

    def test_a_modified_tracked_file_is_refused(self):
        with mock.patch.object(mirror, "dirty_tracked", return_value=["keep_alive.py"]), \
             mock.patch.object(mirror, "head", return_value=("a" * 40, "subject")):
            with self.assertRaises(mirror.Refused) as caught:
                mirror.main([])
        self.assertIn("keep_alive.py", str(caught.exception))
        self.assertIn("no commit contains", str(caught.exception))

    def test_a_head_nobody_else_has_is_refused(self):
        with mock.patch.object(mirror, "dirty_tracked", return_value=[]), \
             mock.patch.object(mirror, "upstream", return_value=("origin/main", 2)), \
             mock.patch.object(mirror, "head", return_value=("a" * 40, "subject")):
            with self.assertRaises(mirror.Refused) as caught:
                mirror.main([])
        self.assertIn("Push first", str(caught.exception))

    def test_a_dirty_tree_is_not_refused_by_a_check(self):
        """``--check`` sends nothing, so nothing about the tree has to be true."""
        with mock.patch.object(mirror, "dirty_tracked", side_effect=AssertionError("asked")), \
             mock.patch.object(mirror, "plan", return_value=([], [])):
            self.assertEqual(mirror.main(["--check"]), 0)


class TheDifferenceIsMeasured(unittest.TestCase):
    """An id comparison is exact for text files and wrong for binary ones."""

    def setUp(self):
        self.path = "keep_alive.py"
        self.size = (ROOT / self.path).stat().st_size

    def _plan(self, *, blob: str, same: bool):
        files = {self.path: "0" * 40}
        with mock.patch.object(
            mirror, "remote", return_value=({self.path: self.size}, {self.path: blob})
        ), mock.patch.object(mirror, "same_bytes", return_value=same):
            return mirror.plan(None, "someone/space", files)

    def test_a_file_whose_id_differs_and_whose_bytes_agree_is_left_alone(self):
        """The Space keeps some binary files itself, in a scheme of its own.

        Measured, not assumed: ten unchanged assets (`.ttf`, `.ogg`) carry a blob
        id the index has never seen while their bytes are identical, so a mirror
        comparing ids alone offered every one of them on every run.
        """
        send, _drop = self._plan(blob="1" * 40, same=True)
        self.assertEqual(send, [], "an unchanged file was offered to the Space")

    def test_a_modified_file_of_the_same_length_is_still_offered(self):
        send, _drop = self._plan(blob="1" * 40, same=False)
        self.assertEqual(send, [self.path])

    def test_a_file_the_space_does_not_have_is_offered_without_being_read(self):
        with mock.patch.object(mirror, "remote", return_value=({}, {})), \
             mock.patch.object(mirror, "same_bytes", side_effect=AssertionError("downloaded")):
            send, _drop = mirror.plan(None, "someone/space", {self.path: "0" * 40})
        self.assertEqual(send, [self.path])

    def test_a_file_that_is_only_on_the_space_is_planned_as_a_drop(self):
        with mock.patch.object(
            mirror, "remote", return_value=({"old.py": 10}, {"old.py": "1" * 40})
        ):
            send, drop = mirror.plan(None, "someone/space", {})
        self.assertEqual(send, [])
        self.assertEqual(drop, ["old.py"])

    def test_the_commit_names_the_commit_it_mirrors(self):
        message, body = mirror.describe("b" * 40, "feat(x): a thing", ["a.py"], ["gone.py"])
        self.assertIn("bbbbbbb", message)
        self.assertIn("feat(x): a thing", message)
        self.assertIn("1 file(s)", body)
        self.assertIn("gone.py", body)


if __name__ == "__main__":
    unittest.main()
