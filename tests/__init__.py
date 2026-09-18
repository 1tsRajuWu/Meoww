"""The whole suite runs against a throwaway database of its own.

This is not tidiness. ``scheduled_messages`` in this project's real ``bot.db`` had
grown **3,680** fixture rows (guild ``777001``, channels ``555001``) because
``test_scheduler.py`` — one of the files that does not use
:class:`tests.support.TempDatabaseMixin` — called ``db.setup()`` and inserted
through the *default* path. The bot running on this same checkout then uploaded
that database on its 25-second cloud loop, and the backup it uploaded is the one
the Render deploy restores from, so test fixtures reached production: the live
instance carries those rows to this day.

``tests/support.py`` already wrote the rule down — *nothing in this suite may
touch a file inside the project* — but a guard a test has to remember to use is
not a guard. This file moves the default for every test at once.

It is ``__init__.py`` and not ``conftest.py`` on purpose. pytest loads a
``conftest``; the check in ``.github/workflows/tests.yml`` runs
``unittest.defaultTestLoader.discover("tests", top_level_dir=".")``, which never
looks at one. Discovery imports ``tests`` before any module inside it, and pytest
imports the package too, so this is the one file both runners run — which is what
makes the guard a guard. (CI starts with no database to protect; a checkout on the
operator's own machine is the case that hurt, and it is run both ways.)

The same shape of bug bit the dashboard preview once (see
``scripts/preview_dashboard.py``), which is why the harness gets its own *copy*
rather than a promise not to write.
"""

import asyncio
import atexit
import os
import shutil
import tempfile

from utils import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the *operator's* database lives. Read only, addressed only by name, so
#: this file can prove the suite is not pointed at it.
LIVE_DB = os.path.join(ROOT, "bot.db")

_WORKDIR = tempfile.mkdtemp(prefix="meow-tests-")
SUITE_DB = os.path.join(_WORKDIR, "bot.db")

# The schema every test expects, built in /tmp. ``db.setup()`` is the same call
# ``test_scheduler`` already makes; the only difference is which file it means.
db.DB_PATH = SUITE_DB
asyncio.run(db.setup())

atexit.register(shutil.rmtree, _WORKDIR, ignore_errors=True)
