"""One policy table, and the guard that keeps it the only one.

Authorization in this project was eleven comparisons against three string
literals, written in five places. Nothing was *wrong* — every comparison was
right when it was written — but the meaning of a scope had no home, so the next
person to read ``auth_scope`` inherited whatever they could infer, and a scope
that gained a second condition had to be changed everywhere it was read.

``utils/principal.py`` is where the meaning lives now. The tests below are the
table itself, one row per scope, and a source walk that fails when ``keep_alive``
starts comparing the string again — which is the only way a table like this
quietly stops being the answer.
"""

import re
import unittest
from pathlib import Path

from utils import principal

ROOT = Path(__file__).resolve().parents[1]

#: A comparison, or a membership test, against the scope stored in the session.
#: Writing it is not the problem — one place has to — but *deciding* with it is.
SCOPE_COMPARISON = re.compile(
    r"""auth_scope"\]\s*(?:==|!=|\bin\b|\bnot\b)"""
)


def _session(**overrides) -> dict:
    base = {"auth_scope": principal.SCOPE_GLOBAL}
    base.update(overrides)
    return base


class TheTable(unittest.TestCase):
    """What each scope may do, in one place, asserted rather than inferred."""

    def test_the_operator_reaches_every_server(self):
        who = principal.from_session(_session())
        self.assertTrue(who.is_operator)
        self.assertTrue(who.may_switch_guild)
        self.assertTrue(who.may_act_in(1))
        self.assertTrue(who.may_act_in(999))

    def test_a_discord_sign_in_reaches_the_servers_it_administers(self):
        who = principal.from_session(
            _session(auth_scope=principal.SCOPE_USER, admin_guild_ids=[7, 8, "9"])
        )
        self.assertTrue(who.is_administered_session)
        self.assertEqual(who.administered, (7, 8, 9))
        self.assertTrue(who.may_act_in(7))
        self.assertTrue(who.may_act_in(9))
        self.assertFalse(who.may_act_in(10))
        self.assertTrue(who.may_switch_guild)

    def test_a_one_time_link_reaches_only_its_own_server(self):
        who = principal.from_session(
            _session(auth_scope=principal.SCOPE_GUILD, guild_id="42")
        )
        self.assertTrue(who.is_scoped_link)
        self.assertEqual(who.guild_id, 42)
        self.assertTrue(who.may_act_in(42))
        self.assertFalse(who.may_act_in(43))
        # Nothing to pick: the link is already pointed at a server.
        self.assertFalse(who.may_switch_guild)

    def test_a_recomputed_list_wins_over_the_session(self):
        """``_visible_guilds`` recomputes every request, so it is passed in."""
        who = principal.from_session(
            _session(auth_scope=principal.SCOPE_USER, admin_guild_ids=[7])
        )
        self.assertFalse(who.may_act_in(8))
        self.assertTrue(who.may_act_in(8, administered=(8,)))

    def test_a_session_with_no_scope_reaches_nothing(self):
        for scope in ("", "wat", None):
            who = principal.from_session(_session(auth_scope=scope))
            with self.subTest(scope=scope):
                self.assertFalse(who.signed_in)
                self.assertFalse(who.is_operator)
                self.assertFalse(who.may_switch_guild)
                self.assertFalse(who.may_act_in(1))
                self.assertEqual(who.label, "unknown" if not scope else "wat")

    def test_a_missing_server_is_never_allowed(self):
        who = principal.from_session(
            _session(
                auth_scope=principal.SCOPE_GUILD,
                guild_id=None,
                admin_guild_ids=[1],
            )
        )
        self.assertFalse(who.may_act_in(None))
        self.assertFalse(who.may_act_in("not a number"))
        self.assertFalse(who.may_act_in(0))

    def test_being_the_owner_is_a_claim_and_not_a_permission(self):
        """The whole point of the last pass: identity is not authorization."""
        who = principal.from_session(
            _session(
                auth_scope=principal.SCOPE_USER,
                owner_candidate=True,
                admin_guild_ids=[5],
            )
        )
        self.assertTrue(who.owner_candidate)
        self.assertFalse(who.is_operator)
        self.assertFalse(who.may_act_in(6))

    def test_it_describes_itself_for_the_console(self):
        who = principal.from_session(
            _session(
                auth_scope=principal.SCOPE_USER,
                user_id="1234",
                guild_id=7,
                discord_user={"name": "R A J"},
            )
        )
        described = who.describe()
        self.assertEqual(described["scope"], "user")
        self.assertEqual(described["label"], "Discord sign-in")
        self.assertEqual(described["user_id"], "1234")
        self.assertEqual(described["guild_id"], "7")
        self.assertEqual(described["name"], "R A J")


class TheStringIsNotComparedAnywhereElse(unittest.TestCase):
    """The guard the table needs to stay the answer.

    Not a rule about the *session key*: ``keep_alive`` writes it, and
    ``utils/principal`` reads it. What must not come back is a decision made from
    it — ``session["auth_scope"] == "global"`` — because that is a second copy of
    a rule that now has one home, and a copy nobody remembers to change.
    """

    def test_keep_alive_never_decides_from_the_raw_scope(self):
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(source.splitlines(), 1)
            if SCOPE_COMPARISON.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "these lines decide from the raw scope instead of asking the "
            "principal, so the policy has a second home: " + "; ".join(offenders),
        )

    def test_the_scope_a_session_is_given_comes_from_the_policy_module(self):
        """The values written into the session are the policy module's own.

        A spelled-out ``session["auth_scope"] = "global"`` is how the fifth
        writer got there last time, and it is the one that then disagrees with the
        reader about what the word means. Only the *assignment* is checked: the
        words "user", "guild" and "global" mean other things elsewhere in this
        file (a mention kind, a picture kind), and a guard that failed for
        ``{"kind": "guild"}`` would be a guard somebody deletes.
        """
        source = (ROOT / "keep_alive.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r'''auth_scope"\][^\n]*=\s*["']''',
            "a scope is spelled out here instead of coming from utils/principal",
        )
        self.assertIn("principal.SCOPE_GLOBAL", source)


if __name__ == "__main__":
    unittest.main()
