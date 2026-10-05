"""The wire format both ends of ``/decat`` speak, and nothing else.

The bot runs on the Space; the model runs on the operator's laptop; the two are
in different processes, on different machines, written by the same repository —
which is exactly the shape that rots. A field renamed on one side and not the
other is not a compile error in either, it is a question that silently never
gets answered.

So the format lives here, once, and both ends import it: the bot builds a
:class:`Job` from a slash command, the agent reads it, the agent sends an
:class:`Answer`, the bot renders it. This module is deliberately **dependency
free** (stdlib only, not even ``discord``) so the Mac half can depend on it
without dragging the bot in, and so the whole protocol is testable on a machine
that has no gateway, no token and no network.

``VERSION`` is the promise that a laptop left running an old copy says so
instead of speaking a slightly different language: the agent sends the version
it speaks, and a mismatch is reported as an upgrade rather than answered as a
parse error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Bumped when a field changes meaning *or* disappears — a new optional field is
#: still version 1, because an old reader ignoring it is the intended behaviour.
VERSION = 1

#: The two modes, lower-cased because Discord hands the choice over exactly as it
#: was typed in ``choices`` and this is what both ends store and compare.
MODE_PUBLIC = "public"
MODE_PRIVATE = "private"
MODES = (MODE_PUBLIC, MODE_PRIVATE)

#: Longest question the protocol carries. Discord's own option limit is 6000,
#: and the bound here is lower on purpose: a question is a sentence somebody
#: wants answered, and the Mac pays for every token of it twice — once to read it
#: and once per verification pass.
MAX_QUESTION = 1500

#: Longest answer, in characters, that will be sent back. Discord's message limit
#: is 2000 and the card is rendered from this, so the agent is the side that
#: trims — it is the side that knows how much of its own answer is padding.
MAX_ANSWER = 4000


def parse_mode(value: Any) -> str:
    """The mode a person named, or the private default for anything else.

    Failing *closed* is the only defensible direction here: an unreadable mode is
    a question whose saving behaviour nobody chose, and saving is the thing that
    cannot be taken back.
    """
    text = str(value or "").strip().lower()
    for mode in MODES:
        if text == mode:
            return mode
    return MODE_PRIVATE


@dataclass(frozen=True)
class Job:
    """One question, on its way from Discord to the laptop."""

    id: str
    question: str
    mode: str = MODE_PRIVATE
    guild_id: int = 0
    channel_id: int = 0
    user_id: int = 0
    user_name: str = ""
    image_url: str | None = None
    created_at: float = 0.0
    deadline: float = 0.0

    @property
    def saves(self) -> bool:
        """Whether the *laptop* may write this exchange to its own history."""
        return self.mode == MODE_PUBLIC

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "v": VERSION,
            "id": self.id,
            "question": self.question,
            "mode": self.mode,
            "guild_id": int(self.guild_id),
            "channel_id": int(self.channel_id),
            "user_id": int(self.user_id),
            "user_name": self.user_name,
            "created_at": float(self.created_at),
            "deadline": float(self.deadline),
        }
        if self.image_url:
            payload["image_url"] = self.image_url
        return payload

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "Job":
        return cls(
            id=str(payload["id"]),
            question=str(payload["question"]),
            mode=parse_mode(payload.get("mode")),
            guild_id=int(payload.get("guild_id") or 0),
            channel_id=int(payload.get("channel_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            user_name=str(payload.get("user_name") or ""),
            image_url=str(payload["image_url"]) if payload.get("image_url") else None,
            created_at=float(payload.get("created_at") or 0.0),
            deadline=float(payload.get("deadline") or 0.0),
        )


@dataclass(frozen=True)
class Answer:
    """One answer, on its way back."""

    id: str
    text: str
    sources: tuple[tuple[str, str], ...] = ()
    model: str = ""
    ms: int = 0
    lane: str = "factual"
    verified: bool = False
    #: True when the answer came from the model's own knowledge because no source
    #: could be reached. The card says so; it is never dressed up as grounded.
    ungrounded: bool = False
    error: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "id": self.id,
            "text": self.text[:MAX_ANSWER],
            "sources": [[title, url] for title, url in self.sources],
            "model": self.model,
            "ms": int(self.ms),
            "lane": self.lane,
            "verified": bool(self.verified),
            "ungrounded": bool(self.ungrounded),
            "error": self.error,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "Answer":
        raw_sources = payload.get("sources") or ()
        sources: list[tuple[str, str]] = []
        for entry in raw_sources:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                sources.append((str(entry[0]), str(entry[1])))
        return cls(
            id=str(payload["id"]),
            text=str(payload.get("text") or "")[:MAX_ANSWER],
            sources=tuple(sources),
            model=str(payload.get("model") or ""),
            ms=int(payload.get("ms") or 0),
            lane=str(payload.get("lane") or "factual"),
            verified=bool(payload.get("verified")),
            ungrounded=bool(payload.get("ungrounded")),
            error=str(payload.get("error") or ""),
        )


class ProtocolError(ValueError):
    """A payload from the other end that this version cannot read."""


def version_problem(payload: dict[str, Any]) -> str:
    """``""`` when the payload is this version, else why it is not.

    A version *older* than ours is still answered where the shape allows it (a new
    optional field is simply absent), so this reports rather than refuses — the
    caller decides, and the one caller that matters logs it as an upgrade owed.
    """
    got = payload.get("v")
    if got is None:
        return "the payload carries no protocol version"
    try:
        got = int(got)
    except (TypeError, ValueError):
        return f"protocol version {got!r} is not a number"
    if got > VERSION:
        return f"that laptop speaks protocol v{got}; this bot knows v{VERSION}"
    return ""
