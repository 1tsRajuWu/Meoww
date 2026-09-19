"""What a server is entitled to, and the Lemon Squeezy licence that proves it.

The pricing page has described three tiers for a while, and until now the whole
thing was a *description*: every system was on in every server, the two paid
buttons pointed at the invite page, and nothing anywhere connected money to a
server. This module is the missing half — the plan model, and the licence that a
server activates with.

Two decisions are kept apart on purpose:

* the **plan model** — which systems and which limits each tier carries, written
  once here so the pricing page, the command and the enforcement points cannot
  disagree about what "Pro" means;
* **enforcement** — whether a server without a licence is actually held to the
  free tier. It ships **off** (``PLAN_ENFORCEMENT``), because switching it on
  takes systems away from servers that are using them today. That is the
  operator's decision, made deliberately, not a side effect of wiring checkout.

The licence itself is a Lemon Squeezy license key, activated **per server**.
Lemon Squeezy counts activations against the limit on the licence, so "up to 5
servers" is the store's own rule rather than a number this bot invents and then
gets wrong; one licence may be activated in as many servers as it allows, and
each server keeps its own row. Validation uses the store's **public** licence
API, so no API key is stored here and no webhook endpoint has to exist.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from utils import db

#: The licence API. Every call in this file goes here and nowhere else.
LICENSE_API = "https://api.lemonsqueezy.com/v1/licenses"

#: Licence API calls are rate limited to 60/minute, and activation happens once
#: per server rather than on a hot path, so a slow answer is never worth hanging
#: a command for.
REQUEST_TIMEOUT = 10


@dataclass(frozen=True)
class Plan:
    """One tier, as the store sells it and as the bot enforces it."""

    id: str
    label: str
    #: Display price, exactly what the store charges for it.
    price: str
    blurb: str
    #: How many servers one licence may be activated in (``None`` = unlimited).
    servers: int | None
    #: Pending scheduled messages per server (``None`` = no plan limit).
    scheduled_per_server: int | None
    #: Systems this plan does **not** include — empty for the paid tiers, which
    #: withhold nothing. Written this way round because the only question the
    #: enforcement points ask is "is this server missing something?", and a set
    #: that is empty for the top tiers makes that question answer itself.
    withheld_modules: frozenset[str] = field(default_factory=frozenset)


#: The systems the pricing page sells as Pro-only, by the ids in
#: ``utils/features.py`` — Support Tickets, Economy & Casino, Market (Live
#: Trading), Starboard and the Activity Tracker. Everything else (moderation,
#: automod, leveling, fun, games, social) is free, which is what makes the free
#: tier a real product rather than a demo.
PRO_MODULES = frozenset({"tickets", "economy", "market", "starboard", "tracker"})

FREE = Plan(
    id="free",
    label="Free",
    price="₹0",
    blurb="Perfect for getting started",
    servers=1,
    scheduled_per_server=0,
    withheld_modules=PRO_MODULES,
)

PRO = Plan(
    id="pro",
    label="Pro",
    price="₹499",
    blurb="Power up your communities",
    servers=5,
    scheduled_per_server=5,
)

ENTERPRISE = Plan(
    id="enterprise",
    label="Enterprise",
    price="₹1,499",
    blurb="For large networks & brands",
    servers=None,
    scheduled_per_server=None,
)

PLANS: dict[str, Plan] = {plan.id: plan for plan in (FREE, PRO, ENTERPRISE)}

#: Where each tier is bought. Spelled out rather than assembled from the plan id
#: at runtime, because a name built by an f-string is a name no grep finds — and
#: this repository's own documentation guard reads ``.env.example`` and refuses to
#: document a variable that appears to be read nowhere.
CHECKOUT_ENV: dict[str, str] = {
    "pro": "LEMONSQUEEZY_CHECKOUT_PRO",
    "enterprise": "LEMONSQUEEZY_CHECKOUT_ENTERPRISE",
}

#: The variant id that maps a licence to a plan, for a store whose products have
#: been renamed — the ids are stable, the name is not.
VARIANT_ENV: dict[str, str] = {
    "pro": "LEMONSQUEEZY_VARIANT_PRO",
    "enterprise": "LEMONSQUEEZY_VARIANT_ENTERPRISE",
}

#: The plan a server with no licence is on. Also the answer to every question
#: about a guild whose row cannot be read — a server that never bought anything
#: is free, and a failed read must not silently grant one.
DEFAULT_PLAN = FREE

_lock = threading.RLock()
#: guild_id -> plan id, the fast path for the command gate.
_CACHE: dict[int, str] = {}


# --------------------------------------------------------------------------- #
# Is any of this switched on?
# --------------------------------------------------------------------------- #
def enforcement_enabled() -> bool:
    """Whether a licence is *required* for the paid systems.

    Off by default, and read fresh every time so an operator can flip it in the
    environment without a code change. Until it is on, every server gets every
    system exactly as before: plans are recorded and displayed, nothing is
    taken away.
    """
    return os.environ.get("PLAN_ENFORCEMENT", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# --------------------------------------------------------------------------- #
# Which plan is this server on?
# --------------------------------------------------------------------------- #
def _row_to_plan(row: dict | None) -> Plan:
    if not row:
        return DEFAULT_PLAN
    # A licence that is not active is not a plan: an expired or disabled key
    # leaves the server on the free tier rather than half-way up a paid one.
    if str(row.get("license_status") or "").lower() != "active":
        return DEFAULT_PLAN
    return PLANS.get(str(row.get("plan") or ""), DEFAULT_PLAN)


def plan_for(guild_id: int | None) -> Plan:
    """The plan this server is on, from its stored licence."""
    if guild_id is None:
        return DEFAULT_PLAN
    try:
        key = int(guild_id)
    except (TypeError, ValueError):
        return DEFAULT_PLAN
    with _lock:
        cached = _CACHE.get(key)
    if cached is not None:
        return PLANS.get(cached, DEFAULT_PLAN)
    try:
        plan = _row_to_plan(db.read_plan_sync(key))
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        print(f"[PLANS] ⚠️ Could not read the plan for {key}: {exc}", flush=True)
        return DEFAULT_PLAN
    with _lock:
        _CACHE[key] = plan.id
    return plan


def invalidate(guild_id: int | None = None) -> None:
    """Drop the cached plan (one server, or every server)."""
    with _lock:
        if guild_id is None:
            _CACHE.clear()
        else:
            try:
                _CACHE.pop(int(guild_id), None)
            except (TypeError, ValueError):
                _CACHE.clear()


def is_paid(guild_id: int | None) -> bool:
    return plan_for(guild_id).id != FREE.id


# --------------------------------------------------------------------------- #
# What that plan allows
# --------------------------------------------------------------------------- #
def feature_allowed(guild_id: int | None, module_id: str) -> bool:
    """Whether ``module_id`` may run for this server.

    Always ``True`` while enforcement is off, which is the shipped state: the
    paid systems are what the pricing page names, and they are switched on for
    everyone until the operator decides otherwise.
    """
    if not enforcement_enabled():
        return True
    return str(module_id) not in plan_for(guild_id).withheld_modules


def blocked_modules(guild_id: int | None) -> frozenset[str]:
    """The paid systems this server does not have. Empty while dormant."""
    if not enforcement_enabled():
        return frozenset()
    return plan_for(guild_id).withheld_modules


def scheduled_cap(guild_id: int | None, default: int) -> int:
    """Pending scheduled messages this server may hold.

    ``default`` is the code's own ceiling (``scheduled.MAX_PENDING_PER_GUILD``)
    and it is what the free tier falls back to as well: a plan may lower the
    queue, and no plan raises it past what the delivery loop is built to handle,
    so Enterprise's "unlimited" means *this queue is not your limit* rather than
    a promise the bot cannot keep.
    """
    if not enforcement_enabled():
        return default
    plan = plan_for(guild_id)
    if plan.scheduled_per_server is None:
        return default
    return max(0, int(plan.scheduled_per_server))


def servers_allowed(guild_id: int | None) -> int | None:
    """Servers one licence may cover — ``None`` for unlimited.

    Displayed rather than enforced here: the store counts activations against
    the licence, so the cap is Lemon Squeezy's own rule and this number is what
    the plan *says*, which is what the page and the command report.
    """
    return plan_for(guild_id).servers


# --------------------------------------------------------------------------- #
# The licence itself
# --------------------------------------------------------------------------- #
def _post(path: str, fields: dict[str, str]) -> dict:
    """POST to the licence API and decode the answer.

    Raises ``OSError`` when the store cannot be reached or answers with
    something that is not one of its own result bodies, so callers can say
    "could not reach Lemon Squeezy" rather than reporting a refusal the buyer
    never got. A documented refusal — including the ``404`` for a key the store
    does not know — is returned as the store's answer.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        f"{LICENSE_API}/{path}",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - the body is already gone
            detail = ""
        # A refusal is an answer, not an outage. The licence API reports a key it
        # does not know as a ``404`` whose body is the documented falsey result
        # (``{"activated": false, "error": "license_key not found."}``), so
        # raising here would tell a buyer who mistyped their key that Lemon
        # Squeezy could not be reached — sending them to check their connection
        # instead of their receipt.
        try:
            refusal = json.loads(detail)
        except ValueError:
            refusal = None
        if isinstance(refusal, dict) and ("activated" in refusal or "valid" in refusal):
            return refusal
        raise OSError(f"Lemon Squeezy answered {exc.code}: {detail[:200]}") from exc
    except Exception as exc:
        raise OSError(f"could not reach Lemon Squeezy ({exc})") from exc
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise OSError("Lemon Squeezy sent something that was not JSON") from exc
    return data if isinstance(data, dict) else {}


def checkout_url(plan: Plan) -> str:
    """The hosted checkout for a tier, or an empty string when there is none.

    Read from the environment because a checkout URL belongs to a *store*: the
    bot has no business guessing a store's domain, and a Buy button pointing at
    somebody else's storefront is worse than no button. An empty value is the
    honest "not for sale yet" the pricing page renders.
    """
    return (os.environ.get(CHECKOUT_ENV.get(plan.id, "")) or "").strip()


def normalise_key(key: str) -> str:
    """A licence key as the store issues it: trimmed, lower case, no spaces."""
    return str(key or "").strip().lower().replace(" ", "")


def key_preview(key: str) -> str:
    """The key as a card may show it: enough to tell two apart, nothing more."""
    key = normalise_key(key)
    if len(key) <= 8:
        return key
    return f"{key[:4]}…{key[-4:]}"


def plan_for_product(meta: dict) -> Plan | None:
    """Which plan a licence belongs to, from the store's own metadata.

    The variant id is checked first when the operator has set one
    (``LEMONSQUEEZY_VARIANT_PRO`` / ``_ENTERPRISE``), because that is the only
    value that stays correct if a product is renamed. The product *name* is the
    fallback that works with no configuration at all, and it is matched on the
    plan's own label so "Pro" in the store is the Pro plan here.
    """
    if not isinstance(meta, dict):
        return None
    variant = str(meta.get("variant_id") or "").strip()
    if variant:
        for plan in (PRO, ENTERPRISE):
            configured = (os.environ.get(VARIANT_ENV[plan.id]) or "").strip()
            if configured and configured == variant:
                return plan
    name = str(meta.get("product_name") or "").strip().lower()
    if not name:
        return None
    for plan in (PRO, ENTERPRISE):
        if plan.label.lower() in name:
            return plan
    return None


def _store(guild_id: int, *, plan: Plan, license_key: str, instance_id: str,
           license_key_row: dict, meta: dict, actor_id: int | None) -> bool:
    row = {
        "plan": plan.id,
        "license_key": license_key,
        "instance_id": instance_id,
        "license_status": str(license_key_row.get("status") or "active"),
        "activation_limit": int(license_key_row.get("activation_limit") or 0),
        "activation_usage": int(license_key_row.get("activation_usage") or 0),
        "customer_email": str(meta.get("customer_email") or ""),
        "activated_by": actor_id,
    }
    if not db.write_plan_sync(int(guild_id), row):
        return False
    invalidate(guild_id)
    return True


def activate(guild_id: int, key: str, *, instance_name: str = "",
             actor_id: int | None = None) -> tuple[bool, str]:
    """Spend one activation of ``key`` on this server.

    Returns ``(ok, message)`` where the message is what to show a person: the
    store's own refusal when it refuses, and this bot's words when the key is
    real but belongs to a product this deployment does not sell.
    """
    if guild_id is None:
        return False, "Run this in the server the licence should cover."
    key = normalise_key(key)
    if not key:
        return False, "Paste the licence key from your Lemon Squeezy receipt."

    name = instance_name or f"Discord server {guild_id}"
    try:
        data = _post("activate", {"license_key": key, "instance_name": name})
    except OSError as exc:
        return False, f"Could not reach Lemon Squeezy to check that key — {exc}"

    if not data.get("activated"):
        refusal = str(data.get("error") or "Lemon Squeezy refused that key.")
        return False, refusal

    license_key_row = data.get("license_key") or {}
    instance = data.get("instance") or {}
    meta = data.get("meta") or {}
    plan = plan_for_product(meta)
    if plan is None:
        # Hand the activation straight back. A refusable mistake must not cost
        # the buyer one of the activations they paid for.
        instance_id = str(instance.get("id") or "")
        if instance_id:
            try:
                _post("deactivate", {"license_key": key, "instance_id": instance_id})
            except OSError as exc:
                print(f"[PLANS] ⚠️ Could not release activation {instance_id}: {exc}", flush=True)
        product = str(meta.get("product_name") or meta.get("variant_name") or "that product")
        return False, (
            f"That key is for **{product}**, which this deployment does not sell. "
            "Ask the operator to add its variant id to `LEMONSQUEEZY_VARIANT_PRO` "
            "or `LEMONSQUEEZY_VARIANT_ENTERPRISE`."
        )

    if not _store(int(guild_id), plan=plan, license_key=key, instance_id=str(instance.get("id") or ""),
                  license_key_row=license_key_row, meta=meta, actor_id=actor_id):
        return False, "That key is valid, but the plan could not be saved. Please try again."

    usage = int(license_key_row.get("activation_usage") or 0)
    limit = int(license_key_row.get("activation_limit") or 0)
    seats = f"{usage} of {limit} servers" if limit else f"{usage} servers"
    return True, f"**{plan.label}** is on for this server — that licence is now active in {seats}."


def deactivate(guild_id: int) -> tuple[bool, str]:
    """Give this server's activation back to the store and forget the plan."""
    try:
        row = db.read_plan_sync(int(guild_id))
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        return False, f"Could not read this server's licence: {exc}"
    if not row or not row.get("license_key"):
        return False, "This server has no licence to remove."

    key = str(row.get("license_key"))
    instance_id = str(row.get("instance_id") or "")
    if instance_id:
        try:
            data = _post("deactivate", {"license_key": key, "instance_id": instance_id})
        except OSError as exc:
            # Keeping the row is deliberate: the activation is still counted by
            # the store, so forgetting it here would leave a licence that says it
            # is free to be used twice.
            return False, f"Could not reach Lemon Squeezy to release that activation — {exc}"
        if not data.get("deactivated"):
            reason = str(data.get("error") or "Lemon Squeezy refused to release it.")
            print(f"[PLANS] ⚠️ Deactivation refused for guild {guild_id}: {reason}", flush=True)
            return False, reason

    if not db.clear_plan_sync(int(guild_id)):
        return False, "Lemon Squeezy released it, but the plan could not be cleared here. Try again."
    invalidate(guild_id)
    return True, "This server is back on the free plan, and that activation is free to use again."


def check(guild_id: int) -> tuple[bool | None, str]:
    """Re-validate the stored key, so a cancellation is visible.

    ``True``/``False`` is what the store said, ``None`` means it could not be
    asked — and this bot does not downgrade a server over a network blip. The
    stored status is refreshed from the answer, so a licence that expired shows
    as such the next time this is called.
    """
    try:
        row = db.read_plan_sync(int(guild_id))
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        return None, f"Could not read this server's licence: {exc}"
    if not row or not row.get("license_key"):
        return None, "This server has no licence."

    key = str(row["license_key"])
    instance_id = str(row.get("instance_id") or "")
    try:
        data = _post("validate", {"license_key": key, **({"instance_id": instance_id} if instance_id else {})})
    except OSError as exc:
        return None, f"Could not ask Lemon Squeezy — {exc}"

    valid = bool(data.get("valid"))
    license_key_row = data.get("license_key") or {}
    status = str(license_key_row.get("status") or ("active" if valid else "expired"))
    try:
        db.write_plan_checked_sync(
            int(guild_id), status=status,
            activation_limit=int(license_key_row.get("activation_limit") or 0),
            activation_usage=int(license_key_row.get("activation_usage") or 0),
        )
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        print(f"[PLANS] ⚠️ Could not record the licence check for {guild_id}: {exc}", flush=True)
    invalidate(guild_id)
    if valid:
        return True, "That licence is active."
    return False, str(data.get("error") or "That licence is no longer valid.")


def summary(guild_id: int) -> dict:
    """Everything a page or a command needs to describe this server's plan."""
    plan = plan_for(guild_id)
    try:
        row = db.read_plan_sync(int(guild_id)) or {}
    except Exception:  # pragma: no cover - depends on the filesystem
        row = {}
    limit = int(row.get("activation_limit") or 0)
    usage = int(row.get("activation_usage") or 0)
    return {
        "plan": plan.id,
        "label": plan.label,
        "price": plan.price,
        "servers": plan.servers,
        "scheduled_per_server": plan.scheduled_per_server,
        "enforced": enforcement_enabled(),
        "licensed": bool(row.get("license_key")) and plan.id != FREE.id,
        "license_preview": key_preview(str(row.get("license_key") or "")) if row.get("license_key") else "",
        "license_status": str(row.get("license_status") or ""),
        "activation_limit": limit,
        "activation_usage": usage,
        "customer_email": str(row.get("customer_email") or ""),
        "blocked_modules": sorted(blocked_modules(guild_id)),
    }
