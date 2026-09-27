"""The dashboard's per-system settings, and the rows they write.

Auto Moderation stopped being a paragraph inside *Moderation & Logs* and became
a system with rules a server owns: how many messages count as a flood, over what
window, for how long the member is timed out, whether invites and links are
blocked, and which domains survive the link filter. The Structure ceilings —
already stored, never controllable — became four number fields on the same kind
of card.

Three claims are worth a law each, because each one fails silently:

* **The registry is the only description.** The page draws its controls from it
  and the endpoint validates against it, so a control the endpoint would refuse
  cannot be drawn, and a value the page saves cannot be one the reader ignores.
* **A write is refused, not clamped.** A page that accepts 500 and prints 200 is
  a page arguing with itself; the operator's fleet lock refuses a system's
  settings for the same reason it refuses its switch.
* **The default is the reader's.** ``features.setting_value`` is what the bot
  reads when a server has never saved anything, and `utils/confirmations` takes
  its ceilings from the same declarations — a second copy of a default is how
  the guard and the page come to disagree about what "unchanged" means.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin
from tests.test_composer import CHANNEL_ID, GUILD_ID, MEMBER_ID, ROLE_ID
from tests.test_dashboard import _DashboardFixture
from utils import confirmations, db, features, modules

#: The seven rules Auto Moderation declares, as the ids a form posts.
AUTOMOD_SETTINGS = {
    "block_invites", "block_links", "link_allowlist", "anti_spam",
    "spam_messages", "spam_window_seconds", "spam_timeout_minutes",
}


class TheDeclarations(unittest.TestCase):
    """What the registry says, before any page or endpoint reads it."""

    def test_auto_moderation_declares_the_rules_its_cog_reads(self):
        from cogs import automod

        self.assertEqual(
            {spec.id for spec in features.settings_for("automod")},
            AUTOMOD_SETTINGS,
        )
        # The keys are the ones the cog has always stored and the dashboard
        # writes: an id renamed here would silently orphan every saved value.
        for spec in features.settings_for("automod"):
            self.assertEqual(
                features.setting_key("automod", spec.id), f"automod_{spec.id}"
            )
        # And the cog's reader is the registry's, not a second list.
        self.assertEqual(
            {spec.id for spec in automod.settings_specs()}, AUTOMOD_SETTINGS
        )

    def test_the_structure_ceilings_are_declared_once(self):
        """``utils/confirmations`` reads the registry rather than repeating it."""
        declared = {
            spec.id: spec.default for spec in features.settings_for("structure")
        }
        self.assertEqual(dict(confirmations.CEILINGS), declared)
        self.assertEqual(
            confirmations.MAX_CEILING,
            max(spec.maximum for spec in features.settings_for("structure")),
        )
        extra = [
            spec for spec in features.settings_for("structure")
            if spec.kind != "int" or spec.minimum is None or spec.maximum is None
        ]
        self.assertEqual(extra, [], "a ceiling with no bounds cannot be validated")

    def test_a_card_carries_the_controls_and_their_bounds(self):
        modules_meta = {m["id"]: m for m in features.dashboard_modules()}
        automod = {spec["id"]: spec for spec in modules_meta["automod"]["settings"]}
        self.assertEqual(set(automod), AUTOMOD_SETTINGS)
        timeout = automod["spam_timeout_minutes"]
        self.assertEqual(timeout["kind"], "int")
        self.assertEqual((timeout["minimum"], timeout["maximum"]), (0, 1440))
        self.assertEqual(automod["link_allowlist"]["kind"], "text")
        self.assertEqual(automod["anti_spam"]["kind"], "bool")
        for spec in automod.values():
            self.assertTrue(spec["label"], spec["id"])
            self.assertTrue(spec["help"], spec["id"])

    def test_a_stored_value_outside_its_declaration_is_the_default(self):
        """Corruption must not become a *larger* allowance than the default."""
        spec = features.get_setting("automod", "spam_messages")
        self.assertEqual(features.setting_value(spec, "12"), 12)
        for raw in ("", None, "lots", "-1", "10_000"):
            self.assertEqual(
                features.setting_value(spec, raw), spec.default, repr(raw)
            )
        boolean = features.get_setting("automod", "block_invites")
        self.assertTrue(features.setting_value(boolean, "on"))
        self.assertFalse(features.setting_value(boolean, "off"))
        self.assertEqual(features.setting_value(boolean, "maybe"), boolean.default)

    def test_a_posted_value_is_validated_against_its_declaration(self):
        """The writer refuses what the reader would fall back on, in words."""
        spec = features.get_setting("automod", "spam_messages")
        self.assertEqual(keep_alive._posted_setting_value(spec, "9"), (9, ""))
        value, error = keep_alive._posted_setting_value(spec, "500")
        self.assertIsNone(value)
        self.assertIn("above 50", error)
        value, error = keep_alive._posted_setting_value(spec, "many")
        self.assertIsNone(value)
        self.assertIn("whole number", error)
        boolean = features.get_setting("automod", "anti_spam")
        self.assertEqual(keep_alive._posted_setting_value(boolean, "0"), (False, ""))
        self.assertEqual(keep_alive._posted_setting_value(boolean, "1"), (True, ""))
        text = features.get_setting("automod", "link_allowlist")
        self.assertEqual(keep_alive._posted_setting_value(text, " github.com "), ("github.com", ""))
        value, error = keep_alive._posted_setting_value(text, "x" * 501)
        self.assertIsNone(value)
        self.assertIn("too long", error)


class _SettingsDoorFixture(_DashboardFixture):
    """A signed-in dashboard whose ``guild_settings`` rows can be read back."""

    def stored(self, key, guild_id=GUILD_ID):
        return db.read_setting_sync(guild_id, key)


class TheSettingsDoor(_SettingsDoorFixture):
    """``/api/module-settings`` — the write, its refusals and its readings."""

    def test_a_threshold_saved_on_the_page_is_the_row_the_bot_reads(self):
        self.sign_in()
        response = self.api(
            "POST", "/api/module-settings",
            {"guild_id": GUILD_ID, "module": "automod", "spam_messages": 12},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertIn("Messages before it acts", payload["said"])
        self.assertEqual(self.stored("automod_spam_messages"), 12)
        # …and the reading the page draws comes from the same row.
        self.assertEqual(
            features.read_setting_values("automod", lambda key: self.stored(key))["spam_messages"],
            12,
        )

    def test_only_the_fields_present_move(self):
        db.write_setting_sync(GUILD_ID, "automod_spam_window_seconds", 20)
        self.sign_in()
        self.api(
            "POST", "/api/module-settings",
            {"guild_id": GUILD_ID, "module": "automod", "spam_messages": 8},
        )
        self.assertEqual(self.stored("automod_spam_messages"), 8)
        self.assertEqual(self.stored("automod_spam_window_seconds"), 20)

    def test_a_value_outside_the_declaration_is_refused_and_writes_nothing(self):
        self.sign_in()
        response = self.api(
            "POST", "/api/module-settings",
            {"guild_id": GUILD_ID, "module": "automod", "spam_timeout_minutes": 9999},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("above 1440", response.get_json()["error"])
        self.assertIsNone(self.stored("automod_spam_timeout_minutes"))

    def test_a_system_without_settings_and_an_unknown_one_are_both_refused(self):
        self.sign_in()
        for module_id in ("tickets", "no-such-system"):
            response = self.api(
                "POST", "/api/module-settings",
                {"guild_id": GUILD_ID, "module": module_id, "spam_messages": 9},
            )
            self.assertEqual(response.status_code, 400, module_id)
            self.assertIn("settings", response.get_json()["error"])

    def test_the_operators_lock_refuses_the_settings_too(self):
        """A system force-closed for every server has nothing to tune."""
        modules.set_operator_override("automod", False)
        self.sign_in()
        response = self.api(
            "POST", "/api/module-settings",
            {"guild_id": GUILD_ID, "module": "automod", "spam_messages": 4},
        )
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True)[:200])
        payload = response.get_json()
        self.assertTrue(payload["operator_locked"])
        self.assertIn("operator", payload["error"])
        self.assertIsNone(self.stored("automod_spam_messages"))

    def test_a_rule_changed_here_drops_the_live_cogs_cached_copy(self):
        """The bot caches per guild; a save that leaves it cached is drift."""
        fake_cog = SimpleNamespace(settings_cache={int(GUILD_ID): {"block_invites": False}})
        # ``create=True``: the stand-in bot is not a discord client and has no
        # ``get_cog`` of its own — production's has one, and the endpoint reads
        # it through ``getattr(..., None)`` for exactly this reason.
        with mock.patch.object(self.bot, "get_cog", lambda name: fake_cog, create=True):
            self.sign_in()
            response = self.api(
                "POST", "/api/module-settings",
                {"guild_id": GUILD_ID, "module": "automod", "block_invites": "1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_cog.settings_cache, {}, "the stale rules were left in place")

    def test_a_form_post_says_it_in_the_pages_own_notice(self):
        """The dashboard's door: a form saves, then lands back on the page."""
        self.sign_in()
        response = self.client.post(
            "/api/module-settings",
            data={
                "csrf_token": "csrf-token",
                "guild_id": str(GUILD_ID),
                "module": "automod",
                "spam_messages": "7",
                "dash_return": "/dashboard/systems",
                "dash_notice": "The settings are saved.",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        self.assertIn(b"Messages before it acts", response.data)
        self.assertEqual(self.stored("automod_spam_messages"), 7)


class TheDefaultRoleDoor(_SettingsDoorFixture):
    """The Server page's own setting — the row ``/defaultrole`` writes."""

    def test_a_role_is_saved_and_cleared(self):
        self.sign_in()
        response = self.api(
            "POST", "/api/server-settings",
            {"guild_id": GUILD_ID, "default_role_id": str(ROLE_ID)},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        self.assertIn("Members", response.get_json()["said"])
        self.assertEqual(self.stored("default_role_id"), ROLE_ID)

        cleared = self.api(
            "POST", "/api/server-settings",
            {"guild_id": GUILD_ID, "default_role_id": ""},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(self.stored("default_role_id"), "off stored a word instead")

    def test_a_role_that_is_not_in_this_server_is_refused(self):
        self.sign_in()
        response = self.api(
            "POST", "/api/server-settings",
            {"guild_id": GUILD_ID, "default_role_id": "424242"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not in this server", response.get_json()["error"])
        self.assertIsNone(self.stored("default_role_id"))

    def test_the_reading_says_what_is_stored_and_whether_discord_would_take_it(self):
        db.write_setting_sync(GUILD_ID, "default_role_id", ROLE_ID)
        reading = keep_alive._default_role_reading(GUILD_ID, keep_alive._role_choices(GUILD_ID))
        self.assertTrue(reading["read"])
        self.assertEqual(reading["role_id"], str(ROLE_ID))
        self.assertEqual(reading["current"], "@Members")

        db.write_setting_sync(GUILD_ID, "default_role_id", 987654)
        missing = keep_alive._default_role_reading(GUILD_ID, keep_alive._role_choices(GUILD_ID))
        self.assertFalse(missing["offered"], "a deleted role was drawn as offered")
        self.assertTrue(missing["current"], "a deleted role was drawn as nothing")


class TheReactionRolesDoor(_DashboardFixture):
    """Mappings the dashboard manages, through the rows the cog reads."""

    def setUp(self):
        super().setUp()
        # The signed-in administrator must be able to give the role out: the
        # endpoint asks the same escalation guard `/addrole` and
        # `/reactionrole add` ask, and a stand-in member has no permissions to
        # read.
        self.guild.members[0].guild_permissions = SimpleNamespace(
            administrator=True, manage_roles=True
        )
        self.guild.me = SimpleNamespace(
            id=999, top_role=SimpleNamespace(position=10), guild_permissions=None
        )
        self.guild.roles.append(_RoleForTest(ROLE_ID, "Members", position=1))

    def sign_in_admin(self):
        self.sign_in()
        with self.client.session_transaction() as session:
            session["user_id"] = MEMBER_ID

    def link(self, guild_id=GUILD_ID, channel_id=CHANNEL_ID, message_id=555001):
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    def test_a_link_mapping_is_saved_with_its_channel(self):
        self.sign_in_admin()
        response = self.api(
            "POST", "/api/reaction-roles",
            {
                "guild_id": GUILD_ID, "action": "add", "message": self.link(),
                "emoji": "🎉", "role_id": str(ROLE_ID),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        rows = db.read_reaction_roles_sync(GUILD_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], 555001)
        self.assertEqual(rows[0]["channel_id"], CHANNEL_ID)
        self.assertEqual(rows[0]["role_id"], ROLE_ID)
        # The stand-in gateway has no loop, so the sentence is the honest
        # middle one: saved, and the reaction could not be added.
        self.assertIn("could not add the reaction", response.get_json()["said"])

    def test_a_link_to_another_server_is_refused(self):
        self.sign_in_admin()
        response = self.api(
            "POST", "/api/reaction-roles",
            {
                "guild_id": GUILD_ID, "action": "add",
                "message": self.link(guild_id=999002), "emoji": "🎉",
                "role_id": str(ROLE_ID),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("another server", response.get_json()["error"])
        self.assertEqual(db.read_reaction_roles_sync(GUILD_ID), [])

    def test_a_member_who_could_not_give_the_role_cannot_wire_it_up(self):
        self.guild.members[0].guild_permissions = None  # unreadable, so refused
        self.sign_in_admin()
        response = self.api(
            "POST", "/api/reaction-roles",
            {
                "guild_id": GUILD_ID, "action": "add", "message": self.link(),
                "emoji": "🎉", "role_id": str(ROLE_ID),
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("permissions", response.get_json()["error"])
        self.assertEqual(db.read_reaction_roles_sync(GUILD_ID), [])

    def test_a_mapping_is_removed_by_its_message_and_emoji(self):
        db.write_reaction_role_sync(GUILD_ID, 555001, CHANNEL_ID, "🎉", ROLE_ID)
        self.sign_in_admin()
        response = self.api(
            "POST", "/api/reaction-roles",
            {"guild_id": GUILD_ID, "action": "remove", "message_id": "555001", "emoji": "🎉"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.read_reaction_roles_sync(GUILD_ID), [])
        # Removing it twice is not an error: the row is already gone.
        again = self.api(
            "POST", "/api/reaction-roles",
            {"guild_id": GUILD_ID, "action": "remove", "message_id": "555001", "emoji": "🎉"},
        )
        self.assertEqual(again.status_code, 200)
        self.assertIn("already gone", again.get_json()["said"])

    def test_an_unreadable_table_says_so_instead_of_drawing_nothing(self):
        with mock.patch.object(db, "read_reaction_roles_sync", side_effect=RuntimeError("locked")):
            reading = keep_alive._reaction_roles_reading(GUILD_ID, [])
        self.assertFalse(reading["read"])
        self.assertEqual(reading["rows"], [])


class _RoleForTest:
    """A role as ``_role_choices`` reads one — the stand-in roles are bare."""

    def __init__(self, rid, name, position=0):
        self.id = rid
        self.name = name
        self.position = position
        self.managed = False

    def is_default(self):
        return False


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
