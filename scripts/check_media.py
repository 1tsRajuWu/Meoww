#!/usr/bin/env python3
"""Ask, right now, whether every picture and sound this bot sends still exists.

The bot already does this on its own weekly clock (``utils/media_health.py``,
started from ``keep_alive.start_media_health``) and posts what it finds to the
operator channel. This is the same one pass, run by hand — for the two moments the
automatic one is not enough:

* **Just after changing a link.** Waiting up to a week to hear whether a picture
  loads is not a workflow. One command answers it.
* **When a card looks wrong and nobody knows which URL it used.** The report names
  the file and line, because "the bot is broken" is not a line to edit.

Nothing here writes anything: it probes, prints, and exits non-zero if something is
dead, so the same command can sit behind a shell conditional later. It needs no
Discord token — unlike the repair script beside it, this never talks to the gateway.

Usage (from the project root)::

    # everything, with the reason and the place for each dead target
    python scripts/check_media.py

    # just the dead ones, one line each
    python scripts/check_media.py --quiet

    # machine-readable, for a dashboard or a diff between two runs
    python scripts/check_media.py --json

    # only what is written into the source, or only what the resolvers make
    python scripts/check_media.py --kind file
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import media_health  # noqa: E402  (after the path fix, on purpose)


class Pass:
    """One filtered view of a verdict: the same pass, seen through ``--kind``."""

    def __init__(self, verdict, kind: str | None, by_kind: dict[str, int]):
        self.at = verdict.at
        self.kind = kind
        # A filtered view has to count only what it looked at, which is why the
        # per-kind totals are handed in rather than guessed from the verdict.
        self.checked = verdict.checked if kind is None else by_kind.get(kind, 0)
        self.dead = {
            name: reason
            for name, reason in sorted(verdict.dead.items())
            if kind is None or verdict.kinds.get(name) == kind
        }
        self.unchecked = {
            name: reason
            for name, reason in sorted(verdict.unchecked.items())
            if kind is None or verdict.kinds.get(name) == kind
        }
        self.places = {name: verdict.places.get(name, "") for name in self.dead}

    @property
    def ok(self) -> int:
        return max(0, self.checked - len(self.dead) - len(self.unchecked))

    def json(self) -> str:
        return json.dumps(
            {
                "at": self.at,
                "kind": self.kind or "all",
                "checked": self.checked,
                "ok": self.ok,
                "dead": len(self.dead),
                "unchecked": len(self.unchecked),
                "targets": [
                    {"name": name, "reason": reason, "url": self.places.get(name, "")}
                    for name in self.dead
                ],
                "not_checked": [
                    {"name": name, "reason": reason} for name, reason in self.unchecked.items()
                ],
            },
            indent=2,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe every media URL the bot sends and report the dead ones.",
    )
    parser.add_argument(
        "--kind",
        choices=("file", "action", "pet"),
        help="only targets of this kind: hardcoded links, or what the resolvers make",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only the dead targets, one per line",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the pass as JSON (implies --quiet)",
    )
    parser.add_argument(
        "--root",
        help="scan this tree instead of the project root",
    )
    return parser


async def main() -> int:
    args = _parser().parse_args()
    verdict = await media_health.check(root=args.root)
    by_kind: dict[str, int] = {}
    for target in media_health.targets(args.root):
        by_kind[target.kind] = by_kind.get(target.kind, 0) + 1
    passed = Pass(verdict, args.kind, by_kind)

    if args.json:
        print(passed.json())
    elif args.quiet:
        for name, reason in passed.dead.items():
            print(f"dead  {name}  {reason}  {passed.places.get(name, '')}".rstrip())
        for name, reason in passed.unchecked.items():
            print(f"????  {name}  no answer: {reason}")
    else:
        print(
            f"Checked {passed.checked} media targets — {passed.ok} fine, "
            f"{len(passed.dead)} dead, {len(passed.unchecked)} could not be asked."
        )
        for name, reason in passed.dead.items():
            print()
            print(f"DEAD  {name}  ({reason})")
            if passed.places.get(name):
                print(f"      {passed.places[name]}")
        for name, reason in passed.unchecked.items():
            print()
            print(f"????  {name}  (no answer: {reason})")
        if not passed.dead and not passed.unchecked:
            print("Every one of them answered with real media.")
        else:
            print()
            print(
                "A dead target written into the source is the link to change; an "
                "action:/pet: target means the resolver (or the provider it asked) "
                "came back empty."
            )

    # A bad link is worth an exit code: "the runner said 0" is what makes a check
    # wireable to a gate later.
    return 1 if passed.dead else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
