"""What Discord actually holds for each scope, as of the last publish pass.

``utils/command_scope`` owns the **rule** — which commands one server should be
offered, given the modules it has switched on. ``bot.py`` owns the **publishing**
of that rule. Neither of them remembers what came back.

That gap is the one the dashboard fell into. Its "Commands in this server" panel
is named for Discord's picker and its copy says a command whose system is off is
"not in this server's picker at all" — and it computed both numbers from the local
module cache, which is what the server *asked for*. So the panel answered the same
ninety whether the republish had landed, was still queued behind a three-second
settle, or had been refused outright by a spent daily write budget. The only place
a refusal was visible at all was a line in the process ring buffer, behind a
button on a different page. An administrator watching a command fail to disappear
could not tell "still publishing" from "Discord said no", and the page's only
gesture at it was a four-second timer that re-read the same local numbers.

So this module is the second half: the publisher records the outcome of every
pass here, and the dashboard reads it. Four facts, each of which is exactly true
or absent — never a guess dressed as a reading:

* **whether Discord's copy was read at all**. A pass that could not read it
  publishes the whole tree from local state (see ``_sync_scope``), which is
  correct behaviour and *not* evidence that the scope is right.
* **how many commands are still to be written**. Zero is the answer to the only
  question the panel is really asked: does Discord offer what this server's
  switches say. It is not inferred from a count of anything.
* **when that pass finished**, and with what delay before the next one.
* **what went wrong**, in Discord's own words, when something did.

In memory, deliberately. A publish is a property of *this process's* conversation
with Discord, and the boot publishes every scope within seconds of coming up — so
"not checked yet in this process" is the honest answer for a process that has not
looked, where a row read back from the database would be a claim about a
conversation that ended at the last restart.

Read from the Flask thread and written from the bot's loop, so every access takes
the lock. It is a dict of small immutable records; the lock is all this needs.

``verified`` is therefore *"Discord's copy was read and the pass finished"*, not
"the read worked": a pass that raises half-way through its own retirements has a
reading and no usable conclusion, which is the same thing to a reader and the
only thing the sentence may claim. The reason travels in ``detail``.
"""
from __future__ import annotations

import threading
import time

#: ``None`` is Discord's global scope, which is not a server and has no id.
GLOBAL_SCOPE = "global"

_lock = threading.Lock()
_records: dict[object, dict] = {}


def _key(scope) -> object:
    """One dictionary key per scope: a server's id, or ``"global"``.

    Takes a guild as readily as an id, because the two callers hold different
    things — ``bot.py`` has a ``discord.Guild`` and the web layer has the id it
    read out of a query string — and a ledger that quietly files those under two
    keys is a ledger nothing ever reads back. It happened during this module's
    first test run: the publisher's records were written under
    ``"<Guild id=… name=…>"`` and the panel looked for an integer, so every
    server would have read as never published.
    """
    if scope is None:
        return GLOBAL_SCOPE
    if not isinstance(scope, (int, str)):
        scope = getattr(scope, "id", scope)
    try:
        return int(scope)
    except (TypeError, ValueError):
        return str(scope)


def record(
    scope,
    *,
    verified: bool,
    pending: int = 0,
    held: int | None = None,
    detail: str = "",
    retry_after: float | None = None,
) -> dict:
    """Write down the outcome of one publish pass, replacing the last one.

    ``held`` is how many commands Discord ended up holding, and is ``None``
    whenever that number is not exactly knowable — a pass refused part-way
    through its writes leaves it ambiguous, and an ambiguous count rendered as a
    figure is worse than no figure at all.
    """
    entry = {
        "at": time.time(),
        "verified": bool(verified),
        "pending": max(0, int(pending or 0)),
        "held": None if held is None else int(held),
        "detail": str(detail or ""),
        "retry_after": retry_after,
    }
    with _lock:
        _records[_key(scope)] = entry
    return entry


def read(scope) -> dict | None:
    """The last pass for one scope, or ``None`` if there has not been one."""
    with _lock:
        entry = _records.get(_key(scope))
    return dict(entry) if entry is not None else None


def forget() -> None:
    """Drop every record. For tests, and for a restart of the bot's own world."""
    with _lock:
        _records.clear()


def _age(seconds: int) -> str:
    """How long ago a pass finished, in words — singular, in every unit.

    Computed where it is read rather than stored, because an age is a fact about
    *now*: a timestamp carried to the page and rendered there is how the Status
    panel came to say "read 1789848173 seconds ago". ``"1 minutes ago"`` reads
    like a bug and makes a reader distrust the numbers beside it, so the plural
    is chosen rather than appended.
    """
    if seconds < 90:
        return f"{seconds} second{'' if seconds == 1 else 's'} ago"
    if seconds < 5400:
        minutes = max(1, seconds // 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = max(1, seconds // 3600)
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def describe(scope, expected: int | None = None, now: float | None = None) -> dict:
    """The panel's line about Discord's copy, and how it should be coloured.

    ``expected`` is how many commands this scope *should* be offered, from
    ``command_scope``. It is used only to name a disagreement that cannot be
    explained by writes still being in flight — the case a bulk overwrite once
    produced, where a server's list is complete and still wrong.

    ``tone`` is one of ``never``, ``unverified``, ``pending`` or ``ok``, and is
    what the template colours the dot with. The words live here rather than in the
    template for the reason the Status panel learned the hard way: this line is
    rendered twice — once by the server, once by the page's own refresh — and two
    renderers that each spell the sentence is how the same box says two different
    things depending on which of them painted it.
    """
    entry = read(scope)
    if entry is None:
        return {
            "tone": "never",
            "age_seconds": None,
            "at": None,
            "held": None,
            "pending": 0,
            "matches": None,
            "text": (
                "Discord's own list has not been read since this process started. "
                "The counts above are what this server's switches say, not what "
                "Discord holds."
            ),
        }

    age_seconds = max(0, int((now if now is not None else time.time()) - entry["at"]))
    age = _age(age_seconds)
    pending = entry["pending"]
    base = {
        "age_seconds": age_seconds,
        # The pass's own timestamp, and the reason it is published at all: a
        # page that has just changed something compares two of *these* to know
        # whether Discord has been read since — never the browser's clock against
        # the server's, which are minutes apart on a host in another region.
        "at": entry["at"],
        "held": entry["held"],
        "pending": pending,
        "matches": None,
    }

    if not entry["verified"]:
        # Two different events arrive here — a read that failed, and a pass that
        # died before it could conclude — and this function cannot tell them
        # apart, so the sentence must not pretend to. It said "could not be
        # read", which is the *reason* for one of them and false for the other:
        # a raise from the retirement step implies the read had already
        # succeeded, and an administrator told to look at Discord's API for a
        # spent write budget would be looking in the wrong place. The reason is
        # in ``detail``, where whoever recorded the pass put it.
        why = f" ({entry['detail']})" if entry["detail"] else ""
        return {
            **base,
            "tone": "unverified",
            "text": (
                f"Discord's copy could not be confirmed{why} — {age}. The counts "
                "above are what this server's switches say, not what Discord holds."
            ),
        }

    if pending:
        # Naming the delay matters: a spent daily write budget refills over
        # minutes, and without the figure a slow drain and a permanent refusal
        # look identical from here.
        wait = ""
        if entry["retry_after"]:
            seconds = int(entry["retry_after"])
            wait = f", and the next pass is in {seconds} second{'' if seconds == 1 else 's'}"
        why = f" — {entry['detail']}" if entry["detail"] else ""
        return {
            **base,
            "tone": "pending",
            "text": (
                f"Discord still needs {pending} command{'' if pending == 1 else 's'} "
                f"written{why}{wait}. Checked {age}."
            ),
        }

    # Three shapes, and exactly one of them may appear: a count that agrees with
    # the switches, a count that does not, and no count at all. Chosen as one of
    # three rather than an opening with a disagreement appended, because appended
    # was a sentence that contradicted itself — the opening said "matching what
    # this server's switches say" and the clause after it said how many more than
    # that it held. That is the very case this function gained its ``expected``
    # argument to name, so the one line it exists to print was the one line that
    # could never be right. Composed whole, each shape is a sentence on its own.
    if entry["held"] is None:
        # A caller may hand this a pair the publisher never records — a finished
        # pass always knows the count — but this is the only place these words
        # exist, so it has to be total for anything, which is what makes it safe
        # as the single author of a line two renderers print.
        held_text = "Discord's list matches what this server's switches say"
        matches = True
    elif expected is None or entry["held"] == expected:
        held_text = (
            f"Discord holds {entry['held']} "
            f"command{'' if entry['held'] == 1 else 's'}, matching what this "
            "server's switches say"
        )
        matches = True
    else:
        difference = abs(expected - entry["held"])
        held_text = (
            f"Discord holds {entry['held']} "
            f"command{'' if entry['held'] == 1 else 's'} — {difference} "
            f"{'more' if entry['held'] > expected else 'fewer'} than this "
            "server's switches say it should"
        )
        matches = False
    return {
        **base,
        "tone": "ok",
        "matches": matches,
        "text": f"{held_text}. Checked {age}.",
    }
