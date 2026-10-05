"""``python -m decat.mac <command>`` — the same command line as the ``decat`` script."""
from __future__ import annotations

import sys

from decat.mac.control import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
