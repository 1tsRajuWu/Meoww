"""Keep the archive out of the test run.

`deleted_features/` is not part of the bot. Its modules import cogs and utils
that no longer live in the tree — `tests/test_uploads.py` archived here imports
`cogs.uploads` — so collecting them turned a plain `pytest` into a collection
error: 532 tests collected, 1 error. Nothing archived is runnable, and the way
to bring one back is to move its files into `cogs/` and `utils/` (see this
folder's README), not to un-ignore it here.
"""
collect_ignore_glob = ["*"]
