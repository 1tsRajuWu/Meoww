"""One sliding-window limiter, used by every door into this deployment.

The logic was written three times — sign-in, the passkey API and the message
composer — and each copy had its own lock, its own purge block and its own idea
of what a "remaining" count means. They agreed by luck rather than by
construction, and the third copy (the composer's) counted a request as a failure
the moment it was *allowed* rather than when it was refused, which is a detail
nobody could have noticed by reading the other two.

What is shared now is the arithmetic: a deque per key holding the timestamps of
the attempts inside the window, with the window's edge being time-based rather
than a counter that has to be reset. That shape was already here and is right —
a fixed counter is either too generous at the boundary or needs a timer of its
own.

What is new is the *reading*. A window is a piece of security state a person can
otherwise not see: "too many failed attempts" arrives as a sentence with no
subject, and the operator cannot tell their own mistyped passkey from a stranger
probing the sign-in page. :meth:`RateLimiter.snapshot` answers what the console
shows: who is currently blocked, how many attempts are tracked, and how often
this window has refused somebody since the process started.

Three properties are deliberate:

* **Per process, not per host.** One Render instance means one window; two
  instances would mean two, and a restart forgets. That is stated rather than
  fixed, because the honest alternative (a shared store) is a database write on
  every sign-in attempt, and the threat this covers — guessing a long random
  passkey — is not made meaningfully easier by an attacker restarting the host.
  The console prints the process's own uptime beside the counts so the reader is
  not misled about how much history they are looking at.
* **Keys are whatever the caller passed**, normally an address. Nothing here
  parses one; ``keep_alive._client_ip`` is the single place that decides what an
  address is, because that decision is about *proxies* rather than about limits.
* **Recording and testing are separate calls** (``record``/``remaining``) as well
  as combined (``allow``). The sign-in form has to be able to say "you have
  three left" without consuming one, and the composer has to count the request it
  just admitted.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

#: A map holding more keys than this is pruned on write. One deque per address,
#: forever, is a slow leak when a scanner walks through a range — and the walk is
#: exactly the traffic a limiter sees most of.
MAX_KEYS = 5000

#: How many blocked keys a snapshot carries. The console is a reading for a
#: person, and a list of two hundred addresses is not one.
MAX_BLOCKED = 10


class RateLimiter:
    """A sliding window over attempts, keyed by whatever the caller names.

    ``limit`` attempts inside any ``window_seconds`` are allowed; the next one is
    refused until the oldest attempt ages out. Time comes from :func:`time.time`
    so a test can freeze it by patching the module's clock.
    """

    def __init__(
        self,
        name: str,
        window_seconds: float,
        limit: int,
        *,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self.limit = limit
        self.max_keys = max_keys
        #: The timestamps themselves, exposed because tests drive the windows
        #: directly (``keep_alive._passkey_attempts.clear()`` and friends) and
        #: because a guard that could not inspect state could not be written.
        self.buckets: dict[str, deque] = defaultdict(deque)
        self.refused = 0
        self.last_refused_at = 0.0
        self._lock = threading.Lock()

    # -- internals --------------------------------------------------------- #
    def _live(self, key: str, now: float) -> deque:
        """The bucket for ``key``, with everything outside the window dropped."""
        bucket = self.buckets[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        return bucket

    def _sweep(self, now: float) -> None:
        """Forget every bucket with nothing left inside the window."""
        for key in [
            key
            for key, stamps in self.buckets.items()
            if not stamps or now - stamps[-1] > self.window_seconds
        ]:
            self.buckets.pop(key, None)

    def _prune(self, now: float) -> None:
        if len(self.buckets) > self.max_keys:
            self._sweep(now)

    # -- the doors --------------------------------------------------------- #
    def remaining(self, key: str) -> int:
        """How many attempts this key has left. Reads; never records."""
        now = time.time()
        with self._lock:
            self._prune(now)
            bucket = self._live(key, now)
            return max(0, self.limit - len(bucket))

    def record(self, key: str) -> None:
        """Count one attempt against a key."""
        now = time.time()
        with self._lock:
            self._prune(now)
            self._live(key, now).append(now)

    def clear(self, key: str) -> None:
        """Forget a key's attempts — what a correct passkey does."""
        with self._lock:
            self.buckets.pop(key, None)

    def allow(self, key: str) -> bool:
        """Test and record in one step: ``True`` when there was room.

        This is the composer's shape rather than the sign-in form's, and the
        difference matters: a sign-in is counted when it *fails*, a message is
        counted when it is *sent*. Both are here so neither caller has to
        reimplement the other's.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            bucket = self._live(key, now)
            if len(bucket) >= self.limit:
                self.refused += 1
                self.last_refused_at = now
                return False
            bucket.append(now)
            return True

    def note_refused(self) -> None:
        """Record a refusal the caller decided on, so the snapshot can count it.

        ``remaining()`` is a question, not a verdict, so the limiter cannot know
        that a refusal followed it — the caller that answered 429 says so.
        """
        with self._lock:
            self.refused += 1
            self.last_refused_at = time.time()

    def clear_all(self) -> None:
        """Forget every window. For tests, and for an operator's ``--reset``."""
        with self._lock:
            self.buckets.clear()

    # -- the reading ------------------------------------------------------- #
    def snapshot(self) -> dict:
        """What this window is doing, for the console.

        ``blocked`` is the part worth having: a key whose bucket is already at
        the limit, with the time it frees up. An address that is blocked after
        one attempt means the deployment's own proxy is naming it wrongly, which
        is a configuration fault this reading is the fastest way to see.
        """
        now = time.time()
        with self._lock:
            self._sweep(now)
            blocked = [
                {
                    "key": key,
                    "attempts": len(stamps),
                    "frees_at": stamps[0] + self.window_seconds,
                    "seconds_left": round(stamps[0] + self.window_seconds - now),
                }
                for key, stamps in self.buckets.items()
                if len(stamps) >= self.limit
            ]
            return {
                "name": self.name,
                "window_seconds": self.window_seconds,
                "limit": self.limit,
                "tracked": len(self.buckets),
                "attempts": sum(len(stamps) for stamps in self.buckets.values()),
                "blocked": sorted(blocked, key=lambda row: (-row["attempts"], row["key"]))[
                    :MAX_BLOCKED
                ],
                "refused": self.refused,
                "last_refused_at": self.last_refused_at or None,
            }
