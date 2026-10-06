#!/usr/bin/env python3
"""Ask, right now, whether every station in the radio table still plays.

The bot already checks its own links on a weekly clock (``utils/media_health.py``,
started from ``keep_alive.start_media_health``) and posts what it finds to the
operator channel. This is the same question asked by hand, for the two moments
the automatic pass is not enough:

* **While the table is being built.** A station list is a list of URLs, and the
  rule this project learned the hard way is that *every URL is fetched before it
  is written down*. ``--from-json`` is that pass: a candidate file in, a verdict
  per candidate out, and only the survivors belong in the table.
* **Just after changing a station.** Waiting up to a week to hear whether the new
  address plays is not a workflow. One command answers it.

Nothing here writes anything: it probes, prints, and exits non-zero if anything
is dead, so the same command can sit behind a shell conditional.

Usage (from the project root)::

    # every station the shipped table holds
    python scripts/probe_stations.py

    # candidates, while building the table
    python scripts/probe_stations.py --from-json /tmp/candidates.json

    # just the dead ones, one line each
    python scripts/probe_stations.py --quiet

    # machine-readable, for a dashboard or a diff between two runs
    python scripts/probe_stations.py --json /tmp/probe-report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import radio_stream  # noqa: E402 — after the path fix above


def _from_json(path: str) -> list[tuple[str, str]]:
    """``(label, url)`` pairs out of a candidate file.

    Tolerant about the shape on purpose: a candidate list is scaffolding written
    by whoever is building the table, and requiring it to match the shipped rows
    field-for-field would mean maintaining a schema for a file that is thrown
    away once the table is committed.
    """
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    out: list[tuple[str, str]] = []
    for row in rows:
        if isinstance(row, str):
            out.append((row, row))
            continue
        url = str(row.get("url") or row.get("stream_url") or "")
        if not url:
            continue
        label = str(row.get("key") or row.get("name") or url)
        out.append((label, url))
    return out


def _from_catalogue() -> list[tuple[str, str]]:
    """Every station the shipped table holds, in table order."""
    from utils import radio_catalogue  # noqa: PLC0415 — imported late so --help is instant

    return [(station.key, station.url) for station in radio_catalogue.CATALOGUE]


def _probe(target: tuple[str, str]) -> dict:
    label, url = target
    verdict = radio_stream.self_check(url)
    return {
        "label": label,
        "url": url,
        "healthy": verdict.healthy,
        "reason": verdict.reason,
        "detail": verdict.detail,
        "unreachable": verdict.unreachable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-json", help="probe a candidate file instead of the table")
    parser.add_argument("--json", dest="report", help="write the full report here")
    parser.add_argument("--quiet", action="store_true", help="print only what failed")
    parser.add_argument("--workers", type=int, default=12, help="parallel probes (default 12)")
    parser.add_argument("--timeout", type=float, default=None, help="seconds per probe")
    args = parser.parse_args()

    if args.timeout:
        radio_stream.PROBE_TIMEOUT = float(args.timeout)

    targets = _from_json(args.from_json) if args.from_json else _from_catalogue()
    if not targets:
        print("nothing to probe")
        return 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(_probe, targets))

    dead = [row for row in results if not row["healthy"]]
    for row in results:
        if row["healthy"] and not args.quiet:
            print(f"  ok   {row['label']:<24} {row['detail']}")
        elif not row["healthy"]:
            kind = "unreachable" if row["unreachable"] else "dead"
            print(f"  {kind:<11} {row['label']:<24} {row['reason']}")
    print(
        f"\n{len(results) - len(dead)}/{len(results)} stations answered"
        + (f", {len(dead)} did not" if dead else "")
    )
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=1)
        print(f"report: {args.report}")
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
