"""Plans, licences, and the line between "recorded" and "enforced".

The pricing page has named three tiers for a while; these tests are about the
half that was missing — a paid plan a server actually has, and a Lemon Squeezy
licence that proves it. Two things are pinned here that a review would otherwise
have to take on trust:

* **Nothing is taken away by accident.** Enforcement ships off, and the tests for
  the dormant state assert that every system stays on for every server whatever
  tier it is on — because the failure mode of getting that wrong is a bot that
  silently stops answering in the servers it is already in.
* **A licence mistake does not cost the buyer a seat.** A key for a product this
  deployment does not sell is valid at the store, so the activation is handed
  straight back; a network failure never downgrades a server; and a released
  activation really is released, so it can be used in another server.
"""
import asyncio
import json
import os
import io
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import db, features, modules, plans


class _FakeResponse:
    """The shape ``urlopen`` returns, which is all ``utils.plans`` reads."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _licence_key(status: str = "active", limit: int = 5, usage: int = 0) -> dict:
    return {
        "id": 1,
        "status": status,
        "key": "38b1460a-5104-4067-a91d-77b872934d51",
        "activation_limit": limit,
        "activation_usage": usage,
        "created_at": "2026-09-19T14:15:07.000000Z",
        "expires_at": None,
    }


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    """A refusal as the store really sends it: a status code *and* a body."""
    return urllib.error.HTTPError(
        "https://api.lemonsqueezy.com/v1/licenses/activate",
        code,
        "Not Found",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def _meta(product: str = "Pro", variant: str = "5") -> dict:
    return {
        "store_id": 1, "order_id": 2, "product_id": 4,
        "product_name": product, "variant_id": variant, "variant_name": "Monthly",
        "customer_id": 6, "customer_name": "John Doe",
        "customer_email": "buyer@example.com",
    }


class StoreAnswering:
    """Records what was sent to the store and answers with a canned body."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, (request.data or b"").decode()))
        if not self.answers:
            raise AssertionError("the store was called more times than the test expects")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)

    @property
    def paths(self) -> list[str]:
        return [url.rsplit("/", 1)[-1] for url, _ in self.calls]


class PlanModelCase(unittest.TestCase):
    """The model itself: what each tier says, and that it names real systems."""

    def test_the_tiers_are_the_ones_the_store_sells(self):
        self.assertEqual(plans.FREE.price, "₹0")
        self.assertEqual(plans.PRO.price, "₹499")
        self.assertEqual(plans.ENTERPRISE.price, "₹1,499")
        self.assertEqual(plans.FREE.servers, 1)
        self.assertEqual(plans.PRO.servers, 5)
        self.assertIsNone(plans.ENTERPRISE.servers, "Enterprise is the unlimited one")

    def test_every_paid_system_is_a_system_the_bot_actually_has(self):
        """A typo here would silently withhold nothing — or the wrong thing.

        The module ids are the ones in ``utils/features.py``, which is also what
        the dashboard switches and the command gates use. Checked by membership
        rather than by eye, because "ticktes" is indistinguishable from "tickets"
        in a dict literal and would simply never match anything.
        """
        known = {feature.id for feature in features.FEATURES}
        self.assertTrue(plans.PRO_MODULES, "the paid tier withholds nothing")
        self.assertLessEqual(plans.PRO_MODULES, known)

    def test_only_the_free_tier_withholds_anything(self):
        """The field means "what this plan is missing", so the paid tiers are empty.

        Written the other way round it reads the same in a literal and inverts the
        meaning: ``paid_modules=PRO_MODULES`` on Pro looks like "Pro includes
        these" and actually withholds them, which is what the first version of
        this model did.
        """
        self.assertEqual(plans.FREE.withheld_modules, plans.PRO_MODULES)
        self.assertEqual(plans.PRO.withheld_modules, frozenset())
        self.assertEqual(plans.ENTERPRISE.withheld_modules, frozenset())


class DormantCase(TempDatabaseMixin, unittest.TestCase):
    """Shipped state: plans are recorded and displayed, nothing is withheld."""

    def setUp(self):
        self.path = self.use_temp_database("dormant.db")
        asyncio.run(db.setup())
        plans.invalidate()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("PLAN_ENFORCEMENT", None)

    def tearDown(self):
        self._env.stop()
        plans.invalidate()
        self.release_temp_database()

    def test_a_free_server_keeps_every_system(self):
        for module_id in sorted(plans.PRO_MODULES):
            self.assertTrue(
                plans.feature_allowed(777001, module_id),
                f"{module_id} was withheld while enforcement is off",
            )
        self.assertEqual(plans.blocked_modules(777001), frozenset())

    def test_the_module_reader_is_unchanged(self):
        """The one place plans are layered on must be invisible while dormant."""
        db.write_guild_module_sync(777001, "economy", True)
        modules.invalidate(777001)
        self.assertTrue(
            modules.is_enabled(777001, "economy"),
            "a stored 'on' was turned off by the plan layer while enforcement is off",
        )
        self.assertTrue(db.write_guild_module_sync(777001, "moderation", False))
        modules.invalidate(777001)
        self.assertFalse(modules.is_enabled(777001, "moderation"))

    def test_the_queue_ceiling_is_still_the_codes_own(self):
        self.assertEqual(plans.scheduled_cap(777001, 25), 25)


class EnforcedCase(TempDatabaseMixin, unittest.TestCase):
    """What the operator gets on the day they flip the switch."""

    def setUp(self):
        self.path = self.use_temp_database("enforced.db")
        asyncio.run(db.setup())
        plans.invalidate()
        self._env = mock.patch.dict(os.environ, {"PLAN_ENFORCEMENT": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        plans.invalidate()
        self.release_temp_database()

    def test_a_free_server_loses_the_paid_systems_and_nothing_else(self):
        self.assertEqual(plans.blocked_modules(777001), plans.PRO_MODULES)
        self.assertFalse(plans.feature_allowed(777001, "economy"))
        for free_module in ("moderation", "automod", "leveling", "fun", "games", "social"):
            self.assertTrue(
                plans.feature_allowed(777001, free_module),
                f"{free_module} is on the free tier and was withheld anyway",
            )

    def test_the_command_gate_sees_the_same_answer_as_the_dashboard(self):
        """One reader, so the switch and the command cannot disagree."""
        db.write_guild_module_sync(777001, "economy", True)
        modules.invalidate(777001)
        self.assertFalse(modules.is_enabled(777001, "economy"))
        self.assertFalse(modules.get_states(777001)["economy"])

    def test_the_dashboard_cannot_switch_a_withheld_system_back_on(self):
        allowed = modules.set_enabled(777001, "economy", True)
        self.assertFalse(allowed, "a plan-withheld system was enabled")
        self.assertNotIn(True, [db.read_guild_modules_sync(777001).get("economy")])

    def test_disabling_a_withheld_system_is_allowed(self):
        """Refusing it would leave a page unable to turn something off."""
        self.assertTrue(modules.set_enabled(777001, "economy", False))

    def test_a_licence_lifts_the_withholding(self):
        db.write_plan_sync(777001, {
            "plan": "pro", "license_key": "k", "instance_id": "i",
            "license_status": "active", "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        self.assertEqual(plans.blocked_modules(777001), frozenset())
        self.assertTrue(plans.feature_allowed(777001, "economy"))

    def test_an_expired_licence_is_not_a_plan(self):
        db.write_plan_sync(777001, {
            "plan": "pro", "license_key": "k", "instance_id": "i",
            "license_status": "expired", "activation_limit": 5, "activation_usage": 5,
        })
        plans.invalidate()
        self.assertEqual(plans.plan_for(777001).id, "free")
        self.assertEqual(plans.blocked_modules(777001), plans.PRO_MODULES)

    def test_a_plan_may_lower_the_queue_but_never_raise_it(self):
        self.assertEqual(plans.scheduled_cap(777001, 25), 0, "Free includes no queue")
        db.write_plan_sync(777001, {
            "plan": "pro", "license_key": "k", "instance_id": "i",
            "license_status": "active", "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        self.assertEqual(plans.scheduled_cap(777001, 25), 5)
        db.write_plan_sync(777001, {
            "plan": "enterprise", "license_key": "k", "instance_id": "i",
            "license_status": "active", "activation_limit": 0, "activation_usage": 1,
        })
        plans.invalidate()
        self.assertEqual(
            plans.scheduled_cap(777001, 25), 25,
            "unlimited means the plan is not the limit, not a promise past the loop's own ceiling",
        )


class LicenceCase(TempDatabaseMixin, unittest.TestCase):
    """Activating, releasing and re-checking a key, against a canned store."""

    GUILD = 777001

    def setUp(self):
        self.path = self.use_temp_database("licence.db")
        asyncio.run(db.setup())
        plans.invalidate()
        self._env = mock.patch.dict(os.environ, {"PLAN_ENFORCEMENT": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        plans.invalidate()
        self.release_temp_database()

    def _store(self, *answers) -> StoreAnswering:
        store = StoreAnswering(list(answers))
        patcher = mock.patch.object(plans.urllib.request, "urlopen", store)
        patcher.start()
        self.addCleanup(patcher.stop)
        return store

    def test_activation_records_the_plan_and_the_seats_the_store_counted(self):
        store = self._store({
            "activated": True, "error": None,
            "license_key": _licence_key(limit=5, usage=2),
            "instance": {"id": "inst-1", "name": "Meow Server (777001)"},
            "meta": _meta("Pro"),
        })
        ok, message = plans.activate(
            self.GUILD,
            " 38B1460A-5104-4067-A91D-77B872934D51 ",
            instance_name="Meow Server (777001)",
            actor_id=99,
        )
        self.assertTrue(ok, message)
        self.assertEqual(store.paths, ["activate"])
        sent = urllib.parse.parse_qs(store.calls[0][1])
        self.assertEqual(sent["instance_name"], ["Meow Server (777001)"])
        self.assertEqual(
            sent["license_key"], ["38b1460a-5104-4067-a91d-77b872934d51"],
            "the key was sent as typed rather than normalised",
        )

        row = db.read_plan_sync(self.GUILD)
        self.assertEqual(row["plan"], "pro")
        self.assertEqual(row["license_status"], "active")
        self.assertEqual(row["activation_limit"], 5)
        self.assertEqual(row["activation_usage"], 2)
        self.assertEqual(row["activated_by"], 99)
        self.assertEqual(
            row["license_key"], "38b1460a-5104-4067-a91d-77b872934d51",
            "the key was stored as typed rather than normalised",
        )
        self.assertIn("2 of 5 servers", message)
        self.assertEqual(plans.plan_for(self.GUILD).id, "pro")
        self.assertEqual(plans.blocked_modules(self.GUILD), frozenset())

    def test_a_refused_key_is_reported_in_the_stores_own_words(self):
        self._store({
            "activated": False,
            "error": "This license key has reached the activation limit.",
            "license_key": _licence_key(limit=5, usage=5),
            "meta": _meta("Pro"),
        })
        ok, message = plans.activate(self.GUILD, "key-at-the-limit")
        self.assertFalse(ok)
        self.assertEqual(message, "This license key has reached the activation limit.")
        self.assertIsNone(db.read_plan_sync(self.GUILD), "a refused key wrote a plan anyway")

    def test_a_key_the_store_never_issued_is_refused_in_its_own_words(self):
        """A typo is a refusal, not an outage.

        The licence API answers a key it does not know with ``404`` and the
        documented falsey body, and the first version of this client treated
        every non-200 as a transport failure — so a buyer who mistyped a
        character was told the store could not be reached, which sends them to
        check their connection instead of their receipt. Verified against the
        real API, which really does answer this way.
        """
        self._store(_http_error(404, '{"activated":false,"error":"license_key not found."}'))
        ok, message = plans.activate(self.GUILD, "0000-0000-0000-0000")
        self.assertFalse(ok)
        self.assertEqual(message, "license_key not found.")
        self.assertNotIn("Could not reach", message)
        self.assertIsNone(db.read_plan_sync(self.GUILD))

    def test_an_outage_that_is_not_a_refusal_is_still_said_plainly(self):
        """The other half of the distinction, so the fix cannot swallow real failures."""
        self._store(_http_error(502, "<html><body>Bad gateway</body></html>"))
        ok, message = plans.activate(self.GUILD, "any-key")
        self.assertFalse(ok)
        self.assertIn("Could not reach Lemon Squeezy", message)
        self.assertIsNone(db.read_plan_sync(self.GUILD))

    def test_a_key_for_a_product_this_deployment_does_not_sell_is_handed_back(self):
        """The failure that would otherwise cost a buyer one of their seats.

        The key is real — the store activates it — but it belongs to something
        this bot does not sell, so the bot has nothing to switch on. Leaving the
        activation spent would mean the customer paid for a seat and cannot use
        it anywhere.
        """
        store = self._store(
            {
                "activated": True, "error": None,
                "license_key": _licence_key(limit=3, usage=1),
                "instance": {"id": "inst-9", "name": "Somewhere Else (1)"},
                "meta": _meta("Meow Bot Brand Kit", variant="77"),
            },
            {"deactivated": True, "error": None, "license_key": _licence_key(), "meta": _meta()},
        )
        ok, message = plans.activate(self.GUILD, "key-for-another-product")
        self.assertFalse(ok)
        self.assertIn("Meow Bot Brand Kit", message)
        self.assertIn("LEMONSQUEEZY_VARIANT_", message)
        self.assertEqual(store.paths, ["activate", "deactivate"], "the activation was not released")
        self.assertIn("inst-9", store.calls[1][1])
        self.assertIsNone(db.read_plan_sync(self.GUILD))

    def test_a_variant_the_operator_named_is_matched_before_the_product_name(self):
        """Renaming a product in the store must not unmap a plan."""
        self._store({
            "activated": True, "error": None,
            "license_key": _licence_key(limit=0, usage=1),
            "instance": {"id": "inst-2", "name": "x"},
            "meta": _meta("Meow Bot Growth", variant="4242"),
        })
        with mock.patch.dict(os.environ, {"LEMONSQUEEZY_VARIANT_ENTERPRISE": "4242"}):
            ok, message = plans.activate(self.GUILD, "enterprise-key")
        self.assertTrue(ok, message)
        self.assertEqual(db.read_plan_sync(self.GUILD)["plan"], "enterprise")
        self.assertIn("enterprise", message.lower())

    def test_an_unreachable_store_is_said_plainly_and_changes_nothing(self):
        self._store(OSError("Connection refused"))
        ok, message = plans.activate(self.GUILD, "any-key")
        self.assertFalse(ok)
        self.assertIn("Could not reach Lemon Squeezy", message)
        self.assertIsNone(db.read_plan_sync(self.GUILD))

    def test_releasing_hands_the_activation_back_to_the_store(self):
        db.write_plan_sync(self.GUILD, {
            "plan": "pro", "license_key": "38b1460a-5104-4067-a91d-77b872934d51",
            "instance_id": "inst-1", "license_status": "active",
            "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        store = self._store({
            "deactivated": True, "error": None,
            "license_key": _licence_key(limit=5, usage=0),
            "instance": {"id": "inst-1", "name": "x"},
            "meta": _meta("Pro"),
        })
        ok, message = plans.deactivate(self.GUILD)
        self.assertTrue(ok, message)
        self.assertEqual(store.paths, ["deactivate"])
        self.assertIn("inst-1", store.calls[0][1])
        self.assertIsNone(db.read_plan_sync(self.GUILD))
        self.assertEqual(plans.plan_for(self.GUILD).id, "free")

    def test_a_refused_release_keeps_the_record_it_could_not_undo(self):
        """Forgetting a licence the store still counts would sell the same seat twice."""
        db.write_plan_sync(self.GUILD, {
            "plan": "pro", "license_key": "k", "instance_id": "inst-1",
            "license_status": "active", "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        self._store({"deactivated": False, "error": "That instance does not exist."})
        ok, message = plans.deactivate(self.GUILD)
        self.assertFalse(ok)
        self.assertEqual(message, "That instance does not exist.")
        self.assertIsNotNone(db.read_plan_sync(self.GUILD), "the row was dropped anyway")

    def test_a_network_blip_does_not_downgrade_a_paying_server(self):
        db.write_plan_sync(self.GUILD, {
            "plan": "pro", "license_key": "k", "instance_id": "inst-1",
            "license_status": "active", "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        self._store(OSError("timed out"))
        checked, note = plans.check(self.GUILD)
        self.assertIsNone(checked, "an unanswered check was reported as a refusal")
        self.assertIn("Could not ask Lemon Squeezy", note)
        self.assertEqual(plans.plan_for(self.GUILD).id, "pro", "the plan was dropped over a timeout")

    def test_a_cancelled_subscription_shows_as_expired_rather_than_staying_pro(self):
        db.write_plan_sync(self.GUILD, {
            "plan": "pro", "license_key": "k", "instance_id": "inst-1",
            "license_status": "active", "activation_limit": 5, "activation_usage": 1,
        })
        plans.invalidate()
        self._store({
            "valid": False, "error": "License key has expired.",
            "license_key": _licence_key(status="expired", limit=5, usage=1),
            "instance": None, "meta": _meta("Pro"),
        })
        checked, note = plans.check(self.GUILD)
        self.assertFalse(checked)
        self.assertEqual(note, "License key has expired.")
        row = db.read_plan_sync(self.GUILD)
        self.assertEqual(row["license_status"], "expired")
        self.assertEqual(row["license_key"], "k", "the stored key was thrown away")
        self.assertEqual(plans.plan_for(self.GUILD).id, "free")

    def test_the_summary_says_what_the_page_and_the_command_show(self):
        summary = plans.summary(self.GUILD)
        self.assertEqual(summary["plan"], "free")
        self.assertEqual(summary["price"], "₹0")
        self.assertEqual(summary["servers"], 1)
        self.assertTrue(summary["enforced"])
        self.assertEqual(sorted(summary["blocked_modules"]), sorted(plans.PRO_MODULES))
        self.assertFalse(summary["licensed"])

    def test_the_key_is_only_ever_shown_masked(self):
        self.assertEqual(plans.key_preview("38b1460a-5104-4067-a91d-77b872934d51"), "38b1…4d51")
        self.assertEqual(plans.key_preview(""), "")


class CommandDeclarationCase(unittest.TestCase):
    """The command exists, and it is the server administrator's to run."""

    def test_the_plan_command_is_declared_for_server_administrators(self):
        from utils import permissions

        self.assertIn("bot plan", permissions.COMMAND_ACCESS)
        self.assertEqual(permissions.COMMAND_ACCESS["bot plan"], permissions.ADMIN)

    def test_the_defining_cog_is_bot_config(self):
        from cogs import bot_config

        self.assertTrue(hasattr(bot_config.BotConfig, "plan"))
        self.assertEqual(bot_config.BotConfig.operator_group.name, "bot")
