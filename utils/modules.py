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
"""
from __future__ import annotations

import threading

from utils import db, features, plans

# guild_id -> {module_id: bool}  (already merged with each feature's default)
CACHE: dict[int, dict[str, bool]] = {}
_lock = threading.RLock()

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
    """Drop cached state (one guild, or everything when ``None``)."""
    with _lock:
        if guild_id is None:
            CACHE.clear()
        else:
            CACHE.pop(guild_id, None)


def apply_cached(guild_id: int, module_id: str, enabled: bool) -> None:
    """Update the in-memory cache only (used when the DB write already happened)."""
    with _lock:
        CACHE.setdefault(guild_id, defaults())[module_id] = bool(enabled)
