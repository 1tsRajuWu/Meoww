"""The queue between Discord and the laptop, which deliberately remembers nothing.

This is the whole of the Space's half of ``/decat``: a slash command puts a
question in, a poll takes it out, an answer comes back, the asker is edited into
the answer, and the job is deleted. There is no table behind it, and that is a
decision rather than an unfinished part — **the hosting Space never keeps a
question or an answer, in either mode.** The transcript for `public` questions
lives on the laptop, which is the machine the operator owns; the words never
touch a database this project runs.

What a restart costs is written down here so it is not discovered later: a job in
flight when the Space reboots is gone, the asker is told the Mac went quiet, and
asking again works. That is the honest price of a relay that stores nothing, and
it is cheaper than the alternative — a table that outlives the process is a table
that holds other people's questions.

Threading, not asyncio, because the two callers are different worlds: the slash
command runs on the gateway's event loop and the Mac's poll runs in a Flask
worker thread. A condition variable is the seam both can share, and
``asyncio.to_thread`` on the bot's side is what keeps the loop free while it
waits.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from decat import protocol


class Refusal(Exception):
    """A question this relay will not take, with the kind of reason why."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class _Slot:
    """One question in flight. Everything here is deleted when it is answered."""

    job: protocol.Job
    taken_at: float | None = None
    answer: protocol.Answer | None = None


class Relay:
    """Questions in, answers out, nothing retained.

    ``clock`` is injectable so the expiry, the cooldown and the heartbeat are all
    testable without sleeping — the same reason ``utils.meetings.upcoming`` grew a
    ``now`` parameter.
    """

    def __init__(
        self,
        *,
        deadline: float = 180.0,
        queue_cap: int = 25,
        concurrency: int = 2,
        cooldown: float = 20.0,
        clock: Callable[[], float] = time.time,
    ):
        self.deadline = float(deadline)
        self.queue_cap = int(queue_cap)
        self.concurrency = int(concurrency)
        self.cooldown = float(cooldown)
        self._clock = clock
        self._lock = threading.Condition()
        self._jobs: dict[str, _Slot] = {}
        self._ready: deque[str] = deque()
        self._inflight: set[str] = set()
        self._last_ask: dict[int, float] = {}
        self._last_poll: float = 0.0
        self._state: str = "unknown"
        self._served = 0
        self._refused = 0
        self._expired = 0
        self._last_ms = 0
        self._last_model = ""
        self._last_lane = ""
        self._ungrounded = 0

    # ── the bot's side ───────────────────────────────────────────────────────

    def enqueue(self, job: protocol.Job) -> str:
        """Accept a question, or refuse it with a reason the asker can read."""
        now = self._clock()
        with self._lock:
            self._reap(now)
            if self._state == "off":
                self._refused += 1
                raise Refusal("off", "the machine is switched off")
            last = self._last_ask.get(int(job.user_id), 0.0)
            waited = now - last
            if self.cooldown and last and waited < self.cooldown:
                self._refused += 1
                raise Refusal("cooldown", f"{int(self.cooldown - waited) + 1}")
            if len(self._jobs) >= self.queue_cap:
                self._refused += 1
                raise Refusal("busy", "the queue is full")
            self._jobs[job.id] = _Slot(job=job)
            self._ready.append(job.id)
            self._last_ask[int(job.user_id)] = now
            self._lock.notify_all()
            return job.id

    def wait(self, job_id: str, timeout: float | None = None) -> protocol.Answer | None:
        """Block until the answer arrives, the deadline passes, or it goes away.

        Called through ``asyncio.to_thread`` so the event loop keeps serving every
        other command — and the gateway heartbeat — while somebody waits.
        """
        limit = self.deadline if timeout is None else float(timeout)
        end = self._clock() + max(0.0, limit)
        with self._lock:
            slot = self._jobs.get(job_id)
            while slot is not None and slot.answer is None:
                remaining = end - self._clock()
                if remaining <= 0:
                    break
                self._lock.wait(min(remaining, 1.0))
                slot = self._jobs.get(job_id)
                self._reap(self._clock())
            if slot is None:
                return None
            return slot.answer

    def settle(self, job_id: str) -> None:
        """Forget a job entirely — its question, its answer and its slot."""
        with self._lock:
            self._jobs.pop(job_id, None)
            self._inflight.discard(job_id)
            self._ready = deque(name for name in self._ready if name != job_id)
            self._lock.notify_all()

    # ── the laptop's side ────────────────────────────────────────────────────

    def take(self, wait: float = 0.0, *, state: str | None = None) -> dict[str, Any] | None:
        """The poll: record that the laptop is awake, then hand it one question.

        The heartbeat is recorded on *every* poll, including the ones that carry
        nothing, because "is the Mac there" and "is there work" are different
        questions and only the first one decides whether the asker is told the
        laptop is offline.
        """
        now = self._clock()
        with self._lock:
            if state in {"on", "off"}:
                self._state = state
            self._last_poll = now
            self._reap(now)
            end = now + max(0.0, float(wait))
            while True:
                if len(self._inflight) < self.concurrency:
                    while self._ready:
                        name = self._ready.popleft()
                        slot = self._jobs.get(name)
                        if slot is None or slot.taken_at is not None:
                            continue
                        slot.taken_at = now
                        self._inflight.add(name)
                        return slot.job.to_wire()
                remaining = end - self._clock()
                if remaining <= 0:
                    return None
                self._lock.wait(min(remaining, 1.0))

    def answer(self, answer: protocol.Answer) -> bool:
        """Record an answer and wake whoever is waiting for it."""
        with self._lock:
            slot = self._jobs.get(answer.id)
            if slot is None:
                return False
            slot.answer = answer
            self._served += 1
            self._last_ms = int(answer.ms or 0)
            self._last_model = answer.model
            self._last_lane = answer.lane
            if answer.ungrounded:
                self._ungrounded += 1
            self._lock.notify_all()
            return True

    # ── the read-out ─────────────────────────────────────────────────────────

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        """What ``/bot decat`` and ``/api/decat/status`` say.

        Deliberately a fact about *counts and timings*: no question, no answer, no
        user id. A status endpoint that could be rendered into a page is a status
        endpoint that must not be able to leak somebody's words.
        """
        moment = self._clock() if now is None else float(now)
        with self._lock:
            age = None if not self._last_poll else max(0.0, moment - self._last_poll)
            return {
                "state": self._state,
                "last_poll_seconds": None if age is None else round(age, 1),
                "waiting": len(self._ready),
                "inflight": len(self._inflight),
                "served": self._served,
                "refused": self._refused,
                "expired": self._expired,
                "ungrounded": self._ungrounded,
                "last_ms": self._last_ms,
                "last_model": self._last_model,
                "last_lane": self._last_lane,
                "queue_cap": self.queue_cap,
                "concurrency": self.concurrency,
                "cooldown": self.cooldown,
            }

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    # ── internals ────────────────────────────────────────────────────────────

    def _reap(self, now: float) -> None:
        """Drop jobs nobody is going to answer. Caller holds the lock.

        An unanswered job past its deadline is not a question waiting for a slow
        answer, it is a question whose asker has already been told the Mac went
        quiet — so it is deleted here rather than left in the table, which is also
        how the relay stays free of text nobody is waiting for.
        """
        dead = [
            name
            for name, slot in self._jobs.items()
            if slot.answer is None
            and slot.job.deadline
            and slot.job.deadline < now
        ]
        for name in dead:
            self._jobs.pop(name, None)
            self._inflight.discard(name)
            self._expired += 1
        if dead:
            self._ready = deque(name for name in self._ready if name in self._jobs)
            self._lock.notify_all()


def new_job(
    question: str,
    *,
    mode: str,
    guild_id: int,
    channel_id: int,
    user_id: int,
    user_name: str,
    image_url: str | None = None,
    now: float,
    deadline: float,
) -> protocol.Job:
    """Build a job with a fresh id and both clocks set."""
    return protocol.Job(
        id=uuid.uuid4().hex[:16],
        question=str(question or "").strip(),
        mode=protocol.parse_mode(mode),
        guild_id=int(guild_id or 0),
        channel_id=int(channel_id or 0),
        user_id=int(user_id or 0),
        user_name=str(user_name or ""),
        image_url=image_url,
        created_at=float(now),
        deadline=float(now) + float(deadline),
    )


#: The process-wide relay. The Flask thread and the gateway share this module, so
#: a question queued by a slash command is the same object the poll takes — which
#: is the entire reason the Space's half needs no database.
_RELAY: Relay | None = None


def shared() -> Relay:
    """The one relay for this process, built on first use."""
    global _RELAY
    if _RELAY is None:
        from decat import config

        _RELAY = Relay(deadline=config.deadline_seconds())
    return _RELAY


def reset_shared() -> None:
    """Drop the shared relay. For tests, and for nothing else."""
    global _RELAY
    _RELAY = None
