"""The operator's word is the last one — and releasing it is not "off".

Asked for in as many words: *"if I turn off a setting in Owner site it should be
force closed everywhere, so no one can use it or turn it on"*. That is a fourth
layer over the three this project already had (the registry's default, the
server's own saved row, and its plan), and it lives inside
``utils.modules.get_states`` for the reason the plan does: the command
interceptor, the per-server command list and four dashboard cards all read
through that one function, so a rule applied at each gate is a rule that some
gate eventually misses.

The half worth testing is the difference between *off* and *no opinion*, because
that is where this kind of feature goes wrong quietly:

  * a locked-off system must be off in a server whose own row says on;
  * the server's own write must be **refused** rather than accepted and ignored —
    a switch that appears to save and then reverts is a defect this project has
    been reported for twice;
  * a write that *agrees* with the operator is kept, so releasing a lock gives a
    server back what it had asked for instead of the registry's default. The
    override is a decision about the fleet, not a reason to forget what the
    fleet's servers wanted;
  * and the page a server's administrator opens has to say so, because the only
    other visible difference is that pressing the switch does nothing.
"""
import asyncio
import unittest

from tests.support import TempDatabaseMixin
from tests.test_dashboard import _DashboardFixture, page_href
from utils import db, features, modules


def non_toggleable() -> str:
    """A registry entry with no switch, for the test that needs a refusal."""
    return next(f.id for f in features.FEATURES if not f.toggleable)


class OperatorOverrideCase(TempDatabaseMixin, unittest.TestCase):
    """The layer itself, driven directly."""

    def setUp(self):
        self.use_temp_database()
        asyncio.run(db.setup())
        modules.invalidate()

    def tearDown(self):
        modules.invalidate()
        self.release_temp_database()

    def test_the_operators_off_beats_a_servers_own_on(self):
        self.assertTrue(modules.set_enabled(111, "economy", True))
        self.assertTrue(modules.is_enabled(111, "economy"))

        self.assertTrue(modules.set_operator_override("economy", False))

        self.assertFalse(
            modules.is_enabled(111, "economy"),
            "a server's own row outranked the operator",
        )
        self.assertEqual(modules.override_for("economy"), False)

    def test_a_server_cannot_turn_a_locked_system_back_on(self):
        self.assertTrue(modules.set_operator_override("economy", False))

        self.assertFalse(
            modules.set_enabled(111, "economy", True),
            "the write was accepted; it is the state that will not follow it",
        )
        self.assertFalse(modules.is_enabled(111, "economy"))
        # And nothing was written underneath: the row a release restores is the
        # one the server had before, not this refused attempt.
        self.assertNotIn("economy", db.read_guild_modules_sync(111))

    def test_the_operators_on_locks_it_on(self):
        self.assertTrue(modules.set_enabled(111, "fun", False))
        self.assertTrue(modules.set_operator_override("fun", True))

        self.assertTrue(modules.is_enabled(111, "fun"))
        self.assertFalse(modules.set_enabled(111, "fun", False))
        self.assertTrue(modules.is_enabled(111, "fun"))

    def test_a_write_that_agrees_is_kept_under_the_lock(self):
        """So releasing a lock gives a server back its *own* answer."""
        self.assertTrue(modules.set_operator_override("economy", False))
        self.assertTrue(
            modules.set_enabled(111, "economy", False),
            "a server asking for the same thing the operator asked for",
        )
        self.assertEqual(
            db.read_guild_modules_sync(111),
            {"economy": False},
            "the agreeing write was not kept underneath the lock",
        )

        self.assertTrue(modules.set_operator_override("economy", None))

        self.assertFalse(
            modules.is_enabled(111, "economy"),
            "releasing the lock lost what that server had asked for",
        )
        # And the guild that never wrote anything is back on the default, which
        # is what makes the previous line a statement about *this* server's
        # choice rather than about the release.
        self.assertTrue(modules.is_enabled(222, "economy"))

    def test_auto_is_not_the_same_as_off(self):
        """Three states rather than two, which is what makes release meaningful."""
        self.assertTrue(modules.set_operator_override("economy", False))
        self.assertFalse(modules.is_enabled(111, "economy"))

        self.assertTrue(modules.set_operator_override("economy", None))

        self.assertIsNone(modules.override_for("economy"))
        self.assertTrue(
            modules.is_enabled(111, "economy"),
            "releasing left the system closed",
        )

    def test_the_override_survives_a_restart(self):
        self.assertTrue(modules.set_operator_override("economy", False))
        # A process restart drops every cache; the row is the fact.
        modules.invalidate()
        self.assertEqual(modules.override_for("economy"), False)
        self.assertFalse(modules.is_enabled(111, "economy"))

    def test_a_core_system_and_an_unknown_id_are_both_refused(self):
        core = non_toggleable()
        self.assertFalse(modules.set_operator_override(core, False), core)
        self.assertFalse(modules.set_operator_override("no-such-system", False))
        self.assertEqual(modules.operator_overrides(), {}, "something was written")

    def test_the_lock_is_read_once_rather_than_per_command(self):
        """Every command asks for module state; the read is cached like the plan's."""
        self.assertTrue(modules.set_operator_override("economy", False))
        modules.invalidate(111)  # one guild's cache only
        self.assertEqual(modules.override_for("economy"), False)
        self.assertEqual(db.read_operator_modules_sync(), {"economy": False})

    def test_the_api_door_refuses_in_its_own_words(self):
        """``/api/toggle``'s whole write, which the dashboard's switch also posts to."""
        import keep_alive

        self.assertTrue(modules.set_operator_override("economy", False))
        payload, status = keep_alive._flip_module(111, "economy", True)

        self.assertEqual(status, 403, payload)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["operator_locked"])
        self.assertIn("operator", payload["error"])
        self.assertFalse(modules.is_enabled(111, "economy"))


    def test_the_bulk_switch_refuses_in_the_same_words_as_one_switch(self):
        """"All off" with a fleet lock on is the operator's decision, not a 500.

        ``modules.set_enabled`` refuses a locked system with the same ``False`` it
        gives a write that did not land, so the bulk door answered *"Could not
        save: leveling, economy, …"* — a server error describing a decision the
        operator made on purpose, and one that contradicts the sentence the
        single switch prints about the very same lock.
        """
        import keep_alive

        self.assertTrue(modules.set_operator_override("economy", True))
        payload, status = keep_alive._flip_all(111, False)

        self.assertEqual(status, 403, payload)
        self.assertTrue(payload["operator_locked"])
        self.assertIn("on for every server by the operator", payload["error"])
        self.assertNotIn("Could not save", payload["error"])

    def test_a_bulk_switch_with_a_fleet_lock_off_is_kept(self):
        """A write that *agrees* with the operator is not a refusal.

        The lock is a decision about the fleet, not a ban on the word — so the
        bulk button that asks for what the operator already said must land, or a
        locked fleet could never be configured to match it.
        """
        import keep_alive

        self.assertTrue(modules.set_operator_override("economy", False))
        payload, status = keep_alive._flip_all(111, False)

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["success"])
        self.assertFalse(modules.is_enabled(111, "economy"))


class TheDashboardSaysWhyItsSwitchWillNotMove(_DashboardFixture):
    """The page a server's administrator actually opens, rendered by its route."""

    def _systems_page(self, html=None):
        self.sign_in()
        response = self.client.get(page_href("systems"))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:400])
        return response.get_data(as_text=True)

    def test_a_locked_system_draws_as_locked_and_names_the_operator(self):
        modules.set_operator_override("economy", False)
        html = self._systems_page()

        self.assertIn("Locked off", html, "the switch does not say it is locked")
        self.assertIn("off for every server", html, "the reason is not on the page")
        # The control is drawn disabled rather than left pressable — the partial
        # does that from `blocked`, which is the same rule a plan already uses.
        section = html.split('id="sys-economy"', 1)[-1][:1200]
        self.assertIn("disabled", section, "a locked switch is still pressable")

    def test_an_unlocked_system_says_nothing_about_a_lock(self):
        html = self._systems_page()
        self.assertNotIn("Locked off", html)
        self.assertNotIn("Locked on", html)
