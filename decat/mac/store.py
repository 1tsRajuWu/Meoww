"""Where a ``public`` question is kept — on the operator's machine, and only there.

This is the entire persistence story of the feature, and it is worth being blunt
about what it is and is not:

* ``public`` — the exchange is appended to ``~/.decat/history/YYYY-MM.jsonl`` on
  **this** laptop, mode ``0600``, next to nothing else. That file is the only copy
  beyond the Discord message itself.
* ``private`` — :meth:`History.append` is never called. Nothing is written here,
  nothing is written on the Space, and nothing is logged anywhere that carries a
  question. The only record left is the ephemeral Discord reply, which vanishes.

A JSONL file rather than a database because the useful properties are the boring
ones: it is appendable without a schema, readable with ``less``, greppable, and
deletable by the person whose machine it is. What is stored is the answer the
asker already saw plus the sources it cited — not the page text, not the model's
intermediate drafts, not the verification pass.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from decat import protocol


class History:
    """Append-only, one file per month, on the laptop that ran the question."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, when: float | None = None) -> Path:
        stamp = time.gmtime(when if when is not None else time.time())
        return self.root / f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}.jsonl"

    def append(self, job: protocol.Job, answer: protocol.Answer, *, when: float | None = None) -> Path | None:
        """Write one exchange. Refuses anything that is not a ``public`` job.

        The refusal is a hard one rather than a caller's discipline: this is the
        single function in the whole feature that puts a question on a disk, so it
        is the one place the promise can be enforced instead of trusted.
        """
        if not job.saves:
            return None
        moment = time.time() if when is None else float(when)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(moment)
        record = {
            "at": moment,
            "id": job.id,
            "mode": job.mode,
            "guild_id": job.guild_id,
            "channel_id": job.channel_id,
            "user_id": job.user_id,
            "user_name": job.user_name,
            "question": job.question,
            "answer": answer.text,
            "lane": answer.lane,
            "verified": answer.verified,
            "ungrounded": answer.ungrounded,
            "sources": [list(pair) for pair in answer.sources],
            "model": answer.model,
            "ms": answer.ms,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # a filesystem without modes is not a reason to lose the record
        return path

    def recent(self, limit: int = 10, *, when: float | None = None) -> list[dict]:
        """The last few exchanges, newest last — for ``decat status``."""
        path = self.path_for(when)
        if not path.exists():
            return []
        rows: list[dict] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows[-limit:]

    def count(self) -> int:
        """How many exchanges this month's file holds, without reading them all."""
        path = self.path_for()
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def summary(job: protocol.Job, answer: protocol.Answer) -> dict:
    """One line about an exchange, for a log that must not contain its words."""
    return {
        "id": job.id,
        "mode": job.mode,
        "lane": answer.lane,
        "ms": answer.ms,
        "sources": len(answer.sources),
        "verified": answer.verified,
        "ungrounded": answer.ungrounded,
        "question_chars": len(job.question),
        "image": bool(job.image_url),
    }


__all__ = ["History", "summary", "asdict"]
