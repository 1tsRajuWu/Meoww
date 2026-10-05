"""What the answer card says, as data — no Discord, so it is checkable anywhere.

The rules a reader can be wronged by are all here, and none of them need a
gateway to hold: that a long answer is *clamped* rather than refused, that the
**question** decides whether the not-a-professional notice is attached, that an
answer with no sources says so instead of looking like one that was checked, and
that every answer discloses that it came from a machine.

Splitting them out of the cog is not tidiness. ``cogs/decat.py`` imports the
Discord library, so a machine that cannot import it — a laptop with the wrong
interpreter, a CI job without the dependency — could not check the part of this
feature that is about *sentences*. Now it can, which is how these rules were
actually verified while they were being written.
"""
from __future__ import annotations

from decat import copy, protocol

#: The embed's description limit is 4096; the notices still have to fit under it.
MAX_ANSWER_CHARS = 3900

#: One colour per lane, so one look says which kind of question this was.
LANE_COLOURS = {"factual": 0x5865F2, "advice": 0xE8A33D, "personal": 0xEB6E9A}


def describe_answer(answer: protocol.Answer, *, question: str) -> dict:
    """The card's contents: ``title``, ``description``, ``colour``, ``footer``, ``sources``."""
    lane = answer.lane if answer.lane in copy.TITLE_BY_LANE else "factual"
    body = (answer.text or "").strip() or "_The Mac sent back nothing to show._"
    if answer.error:
        body = f"{body}\n\n⚠️ {answer.error}".strip()

    warning = copy.warning_for(question)
    room = MAX_ANSWER_CHARS - (len(warning) + 2 if warning else 0)
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    description = f"{body}\n\n{warning}" if warning else body

    grounding = copy.UNGROUNDED_LABEL if answer.ungrounded else copy.GROUNDED_LABEL
    footer = f"{grounding} · {copy.DISCLOSURE.format(model=answer.model or 'a local model')}"
    if answer.ms:
        footer += f" · {(answer.ms / 1000):.1f}s"

    sources = "\n".join(
        f"**{index}.** [{title[:80]}]({url})"
        for index, (title, url) in enumerate(answer.sources[:8], start=1)
    )
    return {
        "title": copy.title_for(lane),
        "description": description[:4096],
        "colour": LANE_COLOURS[lane],
        "footer": footer[:2048],
        "sources": sources[:1024],
    }
