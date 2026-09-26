"""Authoritative module on/off state — one cache, one reader, one writer.

The bot and the Flask dashboard used to keep *two* module caches and *two*
different SQLite write paths. When they drifted (or when a read failed and the
old code silently fell back to "enabled"), a disabled module kept running and a
toggle appeared to revert after a restart. This module is now the only place
that reads or writes module state:

    * ``is_enabled`` / ``get_states`` are the only readers
    * ``set_enabled`` is the only writer (persist first, then update the cache)
    * the cache is shared by both threads, so they cannot disagree

Both processes are the same process here (``python bot.py`` starts Flask in a
daemon thread), so a process-wide lock plus one cached dict is the whole story.

**Three words about a system, and the last one wins.** A system's state is:

1. the registry's default, 2. the server's own row, 3. the server's plan, and
4. the *operator's* override — fleet-wide, and applied last. That order is the
   whole of "the owner's choice is top priority": a system the operator has
   switched off is off in every server, and no server's administrator can
   switch it back on, because :func:`set_enabled` refuses a write that
   contradicts the operator rather than letting a page show "on" for a system
   every other reader hides. It is layered *here*, beside the plan, for the same
   reason the plan is: the command interceptor, the per-server command list and
   four dashboard cards all read through :func:`get_states`, and a rule applied
   at each gate is a rule that some gate will eventually miss.

   A write *agreeing* with the operator is allowed, which is not an oversight:
   the server's own preference is then recorded underneath the lock, so
   releasing the lock restores what that server had asked for instead of the
   default. The override is a decision about the fleet, not a reason to forget
   what the fleet's servers wanted.
"""
from __future__ import annotations

import threading

from utils import db, features, plans

# guild_id -> {module_id: bool}  (already merged with each feature's default)
CACHE: dict[int, dict[str, bool]] = {}
_lock = threading.RLock()

#: module_id -> bool for the operator's fleet-wide overrides. ``None`` means
#: "not read yet" rather than "no overrides" — an empty dict is a real answer
#: (nobody has overridden anything) and the two must not be confused, because
#: answering an unread table as "no overrides" would release every lock.
_OPERATOR: dict[str, bool] | None = None

#: Whether the one failure mode that repeats has been explained. A database
#: restored from a backup taken before ``operator_modules`` existed answers the
#: read with an error until the next ``db.setup()``, and a reader that printed
#: that on every command would bury the log it is trying to be honest in.
_operator_warned = False

_unknown_warned: set[str] = set()

#: (guild, module) pairs already reported as part of a plan the server lacks, so
#: a gated system is explained once instead of on every command that asks.
_plan_blocked_warned: set[tuple[int, str]] = set()


def defaults() -> dict[str, bool]:
    """Baseline state for every toggleable feature."""
    return {f.id: f.enabled_by_default for f in features.FEATURES if f.toggleable}


def _load_from_db(guild_id: int) -> dict[str, bool]:
    """Merge saved rows over the defaults, ignoring retired module ids."""
    states = defaults()
    try:
        saved = db.read_guild_modules_sync(guild_id)
    except Exception as exc:  # pragma: no cover - depends on filesystem/locks
        print(
            f"[MODULES] ⚠️ Could not read module state for guild {guild_id}: {exc}. "
            "Falling back to feature defaults.",
            flush=True,
        )
        return states
    for module_id, enabled in saved.items():
        if module_id in states:
            states[module_id] = bool(enabled)
        # Unknown/retired ids are ignored rather than resurrected.
    return states


def refresh(guild_id: int) -> dict[str, bool]:
    """Force a reload of one guild's state from the database."""
    states = _load_from_db(guild_id)
    with _lock:
        CACHE[guild_id] = states
    return states


def _load_operator_from_db() -> dict[str, bool]:
    """The operator's overrides, read once. Re-raised so a caller can decide."""
    return {str(k): bool(v) for k, v in db.read_operator_modules_sync().items()}


def operator_overrides() -> dict[str, bool]:
    """Cached fleet-wide overrides: ``module_id -> forced on/off``.

    Cached rather than read per call because every command on every server asks
    for module state, and a SQLite read per command is a cost this project has
    already measured once (the plan cache). Reloaded by
    :func:`set_operator_override` and dropped by :func:`invalidate`.
    """
    global _OPERATOR, _operator_warned
    with _lock:
        if _OPERATOR is None:
            try:
                _OPERATOR = _load_operator_from_db()
            except Exception as exc:  # pragma: no cover - depends on filesystem
                if not _operator_warned:
                    _operator_warned = True
                    print(
                        f"[MODULES] ⚠️ Could not read the operator overrides: {exc}. "
                        "Falling back to the servers' own switches, and retrying on "
                        "the next read rather than caching an answer nobody gave.",
                        flush=True,
                    )
                return {}
        return dict(_OPERATOR)


def override_for(module_id: str) -> bool | None:
    """The operator's word about one system, or ``None`` when it is automatic."""
    return operator_overrides().get(str(module_id))


def set_operator_override(module_id: str, enabled: bool | None) -> bool:
    """Force one system on or off for every server, or clear that (``None``).

    Persist first, then update the cache — the same order :func:`set_enabled`
    uses, and for the same reason: this page's whole job is to say what the
    fleet is doing, and a lock held only in memory is a lock that evaporates on
    the next restart while the page keeps claiming it.
    """
    global _OPERATOR
    feature = features.get(module_id)
    if feature is None:
        print(f"[MODULES] ⚠️ Refusing to override unknown module '{module_id}'", flush=True)
        return False
    if not feature.toggleable:
        print(f"[MODULES] ⚠️ '{module_id}' is a core feature and cannot be overridden", flush=True)
        return False
    if enabled is True and module_id in plans.blocked_modules(0):
        # Forcing a plan-gated system *off* is always safe; forcing it *on* is
        # what the licence exists to refuse, and this path has no guild to ask
        # about. Guild ``0`` is the sentinel the console already uses for "no
        # server", which is exactly the right question here: is this system
        # withheld from a server with no plan at all?
        print(
            f"[MODULES] ⚠️ '{module_id}' is plan-gated; the override may close it "
            "for the fleet but not force it open without a licence",
            flush=True,
        )
        return False
    if not db.write_operator_module_sync(module_id, enabled):
        return False
    with _lock:
        if _OPERATOR is None:
            _OPERATOR = {}
        if enabled is None:
            _OPERATOR.pop(str(module_id), None)
        else:
            _OPERATOR[str(module_id)] = bool(enabled)
    return True


def get_states(guild_id: int) -> dict[str, bool]:
    """Cached module states for a guild (loaded from the DB on first use).

    The server's plan is layered on **here** rather than at each gate, because
    three things read module state through this one function — the command
    interceptor, the per-server command list and the dashboard cards — and a
    paid system the server does not have has to be off in one place or those
    three end up disagreeing about the same server. Dormant by default: while
    ``plans.enforcement_enabled()`` is false nothing is withheld and this is a
    plain cache read.
    """
    with _lock:
        cached = CACHE.get(guild_id)
    states = cached if cached is not None else refresh(guild_id)
    gated = _plan_gated(guild_id, states)
    overrides = operator_overrides()
    if not overrides:
        return gated
    # The operator's word is applied *last*, and that is what "the owner's choice
    # is top priority" means in one line: a system they switched off does not
    # come back on because the server's own row says so, and not because the
    # server bought a licence for it either.
    merged = dict(gated)
    merged.update(overrides)
    return merged


def _plan_gated(guild_id: int, states: dict[str, bool]) -> dict[str, bool]:
    """One server's states with its plan's withheld systems treated as off."""
    blocked = plans.blocked_modules(guild_id)
    if not blocked:
        return states
    gated = dict(states)
    for module_id in blocked:
        if gated.get(module_id):
            if (guild_id, module_id) not in _plan_blocked_warned:
                _plan_blocked_warned.add((guild_id, module_id))
                print(
                    f"[MODULES] ⚠️ '{module_id}' is part of a paid plan guild "
                    f"{guild_id} does not have — treating it as off.",
                    flush=True,
                )
            gated[module_id] = False
    return gated


def is_enabled(guild_id: int, module_id: str) -> bool:
    """True when ``module_id`` is running for ``guild_id``.

    Non-toggleable features (core utilities) are always enabled. Unknown ids are
    treated as enabled too: the module system only governs features it knows
    about, and silently blocking an unrecognised name would turn a typo into a
    dead command. ``features.validate()`` and the test-suite catch typos at
    build time instead.
    """
    feature = features.get(module_id)
    if feature is None:
        if module_id not in _unknown_warned:
            _unknown_warned.add(module_id)
            print(
                f"[MODULES] ⚠️ Unknown module id '{module_id}' checked; "
                "treating it as enabled. Add it to utils/features.py.",
                flush=True,
            )
        return True
    if not feature.toggleable:
        return True
    return bool(get_states(guild_id).get(module_id, feature.enabled_by_default))


def set_enabled(guild_id: int, module_id: str, enabled: bool) -> bool:
    """Persist then cache a single toggle. Returns False if the write failed.

    Persisting first is deliberate: the dashboard must never report success for
    a change that was only kept in memory, because it would silently revert on
    the next restart.
    """
    enabled = bool(enabled)
    feature = features.get(module_id)
    if feature is None:
        print(f"[MODULES] ⚠️ Refusing to toggle unknown module '{module_id}'", flush=True)
        return False
    if not feature.toggleable:
        print(f"[MODULES] ⚠️ '{module_id}' is a core feature and cannot be toggled", flush=True)
        return False
    forced = override_for(module_id)
    if forced is not None and forced != enabled:
        # The operator has the last word, so the write is refused rather than
        # accepted and ignored. A switch that appears to save and then reverts
        # is the defect this project has been reported for twice, and the plan
        # gate below refuses for exactly the same reason.
        print(
            f"[MODULES] ⚠️ Refusing to switch '{module_id}' {'on' if enabled else 'off'} "
            f"for guild {guild_id}: the operator has forced it "
            f"{'on' if forced else 'off'} for every server.",
            flush=True,
        )
        return False
    if enabled and not plans.feature_allowed(guild_id, module_id):
        # The dashboard's switch posts here, so refusing is what stops a page
        # from showing "on" for a system every other reader hides. Turning a
        # gated system *off* is still allowed: that is a harmless thing to want
        # and nothing depends on it being refused.
        print(
            f"[MODULES] ⚠️ Refusing to enable '{module_id}' for guild {guild_id}: "
            "it is part of a paid plan this server does not have.",
            flush=True,
        )
        return False
    if not db.write_guild_module_sync(guild_id, module_id, enabled):
        return False
    with _lock:
        CACHE.setdefault(guild_id, defaults())[module_id] = enabled
    return True


def set_many(guild_id: int, mapping: dict[str, bool]) -> list[str]:
    """Toggle several modules at once. Returns the ids that failed."""
    failed: list[str] = []
    for module_id, enabled in mapping.items():
        if not set_enabled(guild_id, module_id, bool(enabled)):
            failed.append(module_id)
    return failed


def invalidate(guild_id: int | None = None) -> None:
    """Drop cached state (one guild, or everything when ``None``).

    Everything includes the operator's overrides, because ``None`` is what every
    caller means by "forget what you know": the next read reloads from the
    database, which is the only place either fact is stored.
    """
    global _OPERATOR
    with _lock:
        if guild_id is None:
            CACHE.clear()
            _OPERATOR = None
        else:
            CACHE.pop(guild_id, None)


def apply_cached(guild_id: int, module_id: str, enabled: bool) -> None:
    """Update the in-memory cache only (used when the DB write already happened)."""
    with _lock:
        CACHE.setdefault(guild_id, defaults())[module_id] = bool(enabled)
