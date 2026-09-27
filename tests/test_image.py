"""What the running image carries, and what it may leave behind.

The image is the deployment, so a file it drops is a page that cannot read — and
the failure is silent, because every one of these readers already falls back
rather than raising: a missing changelog is an empty history, a missing document
is a revision that cannot be computed, a missing font is Pillow's bitmap one.
Each fallback is right on its own; together they make an image that has quietly
lost a page look exactly like a deployment where nothing happened.

That has now happened twice in this repository, and the second time was measured
on the live Space rather than argued: the hub's history page answered *0 entries,
newest first* about a changelog the image did not carry, and the same exclusion
list drops `docs/legal/sub-processors.md`, whose absence empties the revision the
clickwrap gate compares against — and `consent.needs()` answers *no* for every
session when the revision is empty. So the list is checked against the code that
opens these files, in both directions: nothing read is dropped, and the file
still drops something (`tests/` and `.github/` are the whole point of it).
"""
import fnmatch
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"


def patterns() -> list[str]:
    """The live entries of `.dockerignore`, comments and blanks removed."""
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def excluded(path: str) -> bool:
    """Whether `.dockerignore` drops a repository-relative path.

    The subset of the format this file uses, in the order it uses it: a bare name
    is that path **or** a directory above it, `*` and `?` match within one
    segment, and a `!` line puts a path back — which is why this walks the list
    instead of returning at the first match.
    """
    verdict = False
    for pattern in patterns():
        if pattern.startswith("!"):
            if _matches(path, pattern[1:]):
                verdict = False
        elif _matches(path, pattern):
            verdict = True
    return verdict


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        pattern = pattern.rstrip("/")
    if path == pattern or path.startswith(pattern + "/"):
        return True
    return fnmatch.fnmatch(path, pattern)


def files_the_process_reads() -> list[str]:
    """Every file the *running* process opens, taken from the code that opens it.

    Read out of the constants rather than typed here, because the failure this
    guards is a reader added later: a new document in `consent.DOCUMENTS` or a new
    bundled asset moves this list without anybody remembering this file.
    """
    import keep_alive  # noqa: PLC0415 — imported for its own constant
    from utils import card, consent  # noqa: PLC0415 — same

    read = {
        # The history: the hub's page and the console's, both out of this file.
        os.path.relpath(keep_alive.HUB_CHANGELOG, ROOT),
        # The documents the clickwrap shows, and the ones it only hashes.
        *(name for name, _title in consent.DOCUMENTS),
        *consent.BUNDLE_EXTRAS,
        # The fonts a rank card and a market chart are drawn with.
        os.path.relpath(card.FONT_DIR, ROOT) + "/Roboto-Bold.ttf",
        # The cat recordings the soundboard plays, by a path relative to the
        # process's own directory rather than to an import.
        "assets/sounds/purr.ogg",
    }
    return sorted(read)


class TheImageCarriesWhatTheProcessReads(unittest.TestCase):
    """The image is the deployment, and it is built without a server to ask."""

    def test_nothing_the_process_opens_is_dropped(self):
        needed = files_the_process_reads()
        self.assertGreater(len(needed), 5, "the readers were not found")
        for path in needed:
            with self.subTest(path=path):
                self.assertFalse(
                    excluded(path),
                    f"the image drops {path}, which the running process opens — "
                    "and the reader falls back rather than raising, so the page "
                    "that loses it looks like a deployment where nothing happened",
                )

    def test_the_list_still_leaves_something_behind(self):
        """The other direction, so this cannot be passed by emptying the file.

        An image that carries the tests, the workflows and the archived features
        is an image that ships a copy of the repository — which is the defect the
        site's own build refuses by name (a static host handed the repository
        root publishes the source tree along with the pages).
        """
        for path in (
            "tests/test_image.py",
            ".github/workflows/tests.yml",
            "deleted_features/cogs/reaction_roles.py",
            "CONTRIBUTING.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(excluded(path), f"the image carries {path} for no reason")

    def test_a_secret_is_never_in_the_image(self):
        for path in (".env", ".env.example.bak", ".env.local"):
            with self.subTest(path=path):
                self.assertTrue(
                    excluded(path),
                    f"{path} would be baked into the image, where it outlives "
                    "every rotation of it",
                )

    def test_the_matcher_understands_the_shapes_this_file_uses(self):
        """A guard is only a guard if it can find something."""
        self.assertTrue(_matches("docs/legal/x.md", "docs"))
        self.assertTrue(_matches("bot.db-wal", "bot.db-*"))
        self.assertTrue(_matches("a/b.pyc", "*.pyc"))
        self.assertFalse(_matches("docsomething.md", "docs"))
        self.assertFalse(_matches("utils/db.py", "docs"))


if __name__ == "__main__":
    unittest.main()
