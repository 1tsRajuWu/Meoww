"""``/cmd`` — every command the bot offers, scoped to whoever asked.

Three things are being held to account here, and each one is a way the card can
lie without looking wrong:

* **completeness.** A directory that quietly stops at row ninety, or that was
  written against last month's tree, reads exactly like a correct one. So the rows
  are compared with ``utils/permissions.COMMAND_ACCESS`` — the table every command
  must appear in or the suite fails — and the card's own text is compared with the
  tree, so a name it prints must be a command that exists.
* **the scope.** ``utils.permissions`` declares who may run what, and Discord's
  picker is stamped from it before the tree is published; the card has to answer
  the same question the same way, including for the three audiences that have no
  permission bit at all (the server owner, the operator, the market's role).
* **the silence.** A member searching for ``ban`` and being told "nothing found"
  is the failure this feature exists to remove, so the withheld half is pinned:
  named  with its reason where a member can act on one, and *counted* rather than
  named where naming it would advertise one of the operator's own.
"""
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

from cogs import help as help_cog
from utils import command_directory as directory, features, permissions, style

#: Every module on, every permission held: the deployment's worst case for the
#: card's size, and the one the budget has to survive.
ALL_ON = {feature.id: True for feature in features.FEATURES if feature.toggleable}


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeUser:
    def __init__(self, perms, roles=(7,)):
        self.id = 4242
        self.name = "Tester"
        self.display_name = "Tester"
        self.guild_permissions = perms
        self.roles = [FakeRole(role_id) for role_id in roles]
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")


class FakeGuild:
    def __init__(self, name="Hmm"):
        self.id = 555000
        self.name = name
        # Not the user's id, so the owner check is genuinely run rather than
        # short-circuited — see ``_caller_owns_the_server``.
        self.owner_id = 1
        self.icon = None
        self.banner = None
        self.roles = [FakeRole(7), FakeRole(market_role())]


def market_role() -> int:
    """The role the market gate is configured for, read from the rule itself."""
    import utils.market as market_rules

    return market_rules.access_role_ids()[0]


class FakeInteraction:
    """The attributes the card and the viewer builder read, and nothing else."""

    def __init__(self, perms, *, guild_id: int | None = 555000):
        self.user = FakeUser(perms)
        self.guild = FakeGuild()
        self.guild_id = guild_id
        self.client = SimpleNamespace(
            user=FakeUser(discord.Permissions.none()),
            tree=None,
            is_owner=_never_owner,
        )


async def _never_owner(_user):
    return False


def viewer_for(perms, *, states=None, is_owner=False, roles=(7,)) -> directory.Viewer:
    return directory.Viewer(
        states=ALL_ON if states is None else states,
        permissions=perms,
        is_owner=is_owner,
        role_ids=frozenset(roles),
        guild_role_ids=frozenset({7, market_role()}),
    )


def rendered_text(view) -> str:
    """Every text component of a card, as one string.

    Walks the view rather than reading the page dictionary, because the point of
    the card tests is what Discord would actually be sent: a page that never
    reached a component is a page the member never sees.
    """
    found: list[str] = []
    stack = list(getattr(view, "children", []) or [])
    while stack:
        node = stack.pop(0)
        content = getattr(node, "content", None)
        if isinstance(content, str):
            found.append(content)
        stack.extend(list(getattr(node, "children", []) or []))
    return "\n".join(found)


def rows_of(view) -> list:
    """Every ActionRow anywhere in the card."""
    found: list = []

    def walk(node):
        for child in getattr(node, "children", []) or []:
            if isinstance(child, discord.ui.ActionRow):
                found.append(child)
            walk(child)

    walk(view)
    return found


class DirectoryCase(unittest.IsolatedAsyncioTestCase):
    """The real cogs, loaded for real — the tree the card is built from."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")
        # What ``bot.py`` does before publishing: the picker's visibility comes
        # from the table, so the card reading the same table is reading what
        # Discord was told.
        permissions.apply_declared_permissions(self.bot.tree)
        self.rows = directory.commands(self.bot.tree)

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def paths(self) -> set[str]:
        return {command.path for command in self.rows}

    def row(self, path: str) -> directory.Command:
        for command in self.rows:
            if command.path == path:
                return command
        self.fail(f"/{path} is not in the directory: {sorted(self.paths())}")

    def card(self, viewer, *, rows=None, **kwargs):
        return help_cog.build_cmd_card(
            self.bot, FakeInteraction(viewer.permissions), viewer=viewer,
            rows=rows if rows is not None else self.rows,
            scoped=directory.scope(rows if rows is not None else self.rows, viewer),
            **kwargs,
        )


class TestTheDirectoryIsTheTree(DirectoryCase):
    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_the_walk_found_the_commands(self):
        """A check that read nothing would pass every test below for free."""
        self.assertGreater(len(self.rows), 100)

    def test_every_declared_audience_is_a_row(self):
        """The table is the list of commands, and a row is never missing from it.

        ``utils/permissions`` already refuses an undeclared command, so this is
        the same law read from the other side: if a command exists, this card
        prints it — there is no second list that could fall behind.
        """
        declared = set(permissions.COMMAND_ACCESS)
        self.assertEqual(declared - self.paths(), set())

    def test_no_row_is_undeclared(self):
        self.assertEqual(permissions.undeclared(self.bot.tree), [])

    def test_the_registry_declares_no_row_that_does_not_exist(self):
        """A declared row that is not a command is a row printed for nobody."""
        self.assertEqual(directory.declared_but_absent(self.bot.tree), ())

    def test_every_row_knows_its_system_and_its_audience(self):
        for command in self.rows:
            with self.subTest(path=command.path):
                self.assertTrue(command.system_id)
                self.assertTrue(command.system)
                self.assertIn(command.audience, permissions.PICKER_VISIBILITY)

    def test_a_command_two_systems_gate_is_listed_once_and_says_so(self):
        """``/slots`` is in Economy & Casino, and says it needs Mini-Games too.

        The alternative — printing it twice — makes two of the counts on the card
        add up to more than the number above them, and leaving the second system
        unsaid tells a member that opening Mini-Games should not have lost it.
        """
        slots = self.row("slots")
        self.assertEqual(slots.system_id, "economy")
        self.assertIn("Mini-Games Hub", slots.note)
        games = [command.path for command in self.rows if command.system_id == "games"]
        self.assertNotIn("slots", games)

    def test_the_notes_are_the_registrys_own_sentences(self):
        """The card's descriptions cannot drift, because it does not own them."""
        registry = dict(features.feature_entries(features.get("market")))
        self.assertEqual(self.row("market buy").note, registry["/market buy"])
        self.assertTrue(self.row("market buy").note)

    def test_a_command_the_registry_says_nothing_about_is_still_listed(self):
        """Failing open, like ``command_scope``: a listing may not lose a command."""
        quiet = [command for command in self.rows if not command.note]
        self.assertTrue(quiet, "every command has a note, so this guard proves nothing")
        # `/balance` has no note in the registry today; whatever the quiet ones
        # are, each is a real command in the tree.
        for command in quiet[:5]:
            self.assertIn(command.path, self.paths())


class TestTheScopeIsTheTruth(DirectoryCase):
    """Who may run what, answered the way Discord answers it."""

    def test_every_audience_asks_for_at_most_one_permission_bit(self):
        """Which makes "the member must hold all of it" an exact sentence.

        Discord's ``default_member_permissions`` is a bit set, and whether a
        member must hold all of it or any of it is a question the API does not
        answer in the docs; this card says *all*. One bit per audience is what
        makes that unambiguous, so a future audience declaring two would have to
        decide this on purpose rather than inherit an answer.
        """
        for name, value in permissions.PICKER_VISIBILITY.items():
            bits = int(getattr(value, "value", 0) or 0).bit_count()
            with self.subTest(audience=name):
                self.assertLessEqual(bits, 1)

    def test_a_system_switched_off_here_is_the_reason_even_for_an_admin(self):
        """The server's own switch comes first, because it is the fixable one."""
        viewer = viewer_for(discord.Permissions.all(), states={})
        scoped = directory.scope(self.rows, viewer)
        held = {entry.command.path: entry.reason for entry in scoped.withheld}
        self.assertEqual(held["market board"].kind, directory.OFF)
        self.assertIn("Market", held["market board"].text)
        # …and an administrator is not special about it: nobody can run a system
        # this server has switched off.
        self.assertNotIn(
            "market board", {command.path for command in scoped.usable}
        )

    def test_a_permission_audience_is_exactly_the_bit_it_declares(self):
        banned = discord.Permissions(ban_members=True)
        viewer = viewer_for(banned)
        scoped = directory.scope(self.rows, viewer)
        usable = {command.path for command in scoped.usable}
        self.assertIn("ban", usable)
        self.assertIn("unban", usable)
        self.assertNotIn("kick", usable)  # a different bit, and not held
        held = {entry.command.path: entry.reason for entry in scoped.withheld}
        self.assertEqual(held["kick"].kind, directory.PERMISSION)
        self.assertEqual(held["kick"].text, "needs **Kick Members**")

    def test_an_administrator_holds_every_permission_audience(self):
        """Discord's own rule: the administrator bit is a superset of the rest."""
        viewer = viewer_for(discord.Permissions(administrator=True))
        usable = {command.path for command in directory.scope(self.rows, viewer).usable}
        self.assertIn("kick", usable)
        self.assertIn("purge", usable)
        self.assertIn("setlevel", usable)

    def test_the_owner_audience_is_the_owner_and_not_administrator(self):
        """Discord cannot express "is the owner", so the bit *approximates* it.

        The picker therefore shows ``/eco-admin set`` to an administrator who is
        not the server owner, and the command refuses them at use time. The card
        says what is true, which is the whole reason it evaluates the real check
        instead of reading the picker's bit back.
        """
        administrator = discord.Permissions(administrator=True)
        not_owner = viewer_for(administrator, is_owner=False)
        self.assertNotIn(
            "eco-admin set", {c.path for c in directory.scope(self.rows, not_owner).usable}
        )
        held = {entry.command.path: entry.reason for entry in directory.scope(self.rows, not_owner).withheld}
        self.assertEqual(held["eco-admin set"].kind, directory.OWNER)
        owner = viewer_for(administrator, is_owner=True)
        self.assertIn(
            "eco-admin set", {c.path for c in directory.scope(self.rows, owner).usable}
        )

    def test_the_market_audience_is_the_configured_role(self):
        """A gate rather than a permission, so an administrator is not an exception."""
        administrator = discord.Permissions(administrator=True)
        without = viewer_for(administrator, roles=(7,))
        held = {entry.command.path: entry.reason for entry in directory.scope(self.rows, without).withheld}
        self.assertEqual(held["market buy"].kind, directory.MARKET)
        with_role = viewer_for(administrator, roles=(7, market_role()))
        self.assertIn("market buy", {c.path for c in directory.scope(self.rows, with_role).usable})

    def test_the_market_reports_a_server_that_does_not_have_the_role(self):
        """``missing`` is a different answer from ``role``, and it is fixable."""
        viewer = directory.Viewer(
            states=ALL_ON,
            permissions=discord.Permissions(administrator=True),
            role_ids=frozenset(),
            guild_role_ids=frozenset({7}),
        )
        held = {entry.command.path: entry.reason for entry in directory.scope(self.rows, viewer).withheld}
        self.assertIn("is not on this server", held["market buy"].text)

    def test_an_audience_nobody_declared_is_offered(self):
        """Failing open: a name the table does not know may not cost a member a command."""
        invented = directory.Command(
            path="invented", root="invented", system_id="core", system="Always available",
            icon="🧭", note="", audience="nobody_declared_this",
        )
        scoped = directory.scope([invented], viewer_for(discord.Permissions.none()))
        self.assertEqual(scoped.withheld, ())
        self.assertEqual(len(scoped.usable), 1)

    def test_the_operator_rows_are_the_only_private_refusals(self):
        """A refusal that may not be named is one reason, and it is stated once."""
        for entry in directory.scope(
            self.rows, viewer_for(discord.Permissions.none())
        ).withheld:
            with self.subTest(path=entry.command.path):
                self.assertEqual(
                    entry.reason.private, entry.reason.kind == directory.OPERATOR
                )


class TestTheSearch(unittest.TestCase):
    """Search over the rows, ranked, with the slash thrown away."""

    @classmethod
    def setUpClass(cls):
        # Hand-built rows: a search is a property of the rows, and building them
        # here keeps this class free of a client and a cog load.
        cls.rows = (
            directory.Command("balance", "balance", "economy", "Economy & Casino", "💰",
                              "", permissions.EVERYONE),
            directory.Command("market buy", "market", "market", "Market", "📈",
                              "Buy an instrument with coins", permissions.MARKET),
            directory.Command("market sell", "market", "market", "Market", "📈",
                              "Sell a position", permissions.MARKET),
            directory.Command("kick", "kick", "moderation", "Moderation & Logs", "🛡️",
                              "Kick a member from the server", permissions.KICK),
        )

    def test_a_name_finds_its_command(self):
        self.assertEqual([c.path for c in directory.search(self.rows, "balance")], ["balance"])

    def test_the_slash_is_thrown_away(self):
        """A member copies what a card printed, and what it printed had a slash."""
        self.assertEqual([c.path for c in directory.search(self.rows, "/market buy")], ["market buy"])

    def test_a_word_from_the_description_finds_it(self):
        self.assertEqual(
            [c.path for c in directory.search(self.rows, "instrument")], ["market buy"]
        )

    def test_a_system_name_finds_its_commands(self):
        found = [c.path for c in directory.search(self.rows, "market")]
        self.assertEqual(found, ["market buy", "market sell"])
        self.assertEqual([c.path for c in directory.search(self.rows, "moderation")], ["kick"])

    def test_an_exact_name_outranks_a_contained_one(self):
        rows = self.rows + (
            directory.Command("unban", "unban", "moderation", "Moderation & Logs", "🛡️",
                              "", permissions.BAN),
        )
        self.assertEqual(directory.search(rows, "ban")[0].path, "unban", "only /unban contains it")
        rows = rows + (
            directory.Command("ban", "ban", "moderation", "Moderation & Logs", "🛡️",
                              "Ban a member", permissions.BAN),
        )
        self.assertEqual(directory.search(rows, "ban")[0].path, "ban")

    def test_every_word_has_to_land_somewhere(self):
        self.assertEqual(directory.search(self.rows, "market nonexistent"), ())
        self.assertEqual(directory.search(self.rows, ""), ())

    def test_it_is_case_insensitive(self):
        self.assertEqual([c.path for c in directory.search(self.rows, "BALANCE")], ["balance"])


class TestTheCard(DirectoryCase):
    """The card itself — what Discord is sent, and what it promises."""

    def test_every_command_the_card_names_is_real(self):
        """The drift guard: a printed name that is not a command is a dead end."""
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        roots = {command.root for command in self.rows}
        printed: set[str] = set()
        for page in (self.card(viewer).pages.values()):
            printed |= set(re.findall(r"/([a-z0-9][a-z0-9-]*)", page[1]))
        self.assertGreater(len(printed), 40, "the card printed almost nothing")
        for root in sorted(printed):
            with self.subTest(command=root):
                self.assertIn(root, roots)

    def test_a_page_that_fits_names_every_command_that_is_usable(self):
        """The promise of the feature, measured rather than asserted: it fits.

        An administrator with every permission in a server with every switch on is
        the worst case there is, and the card has to print every one of those names
        — the budget trim below it is a guard against a future bot, not a licence
        to drop rows today.
        """
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        scoped = directory.scope(self.rows, viewer)
        text = rendered_text(self.card(viewer))
        self.assertNotIn("No room left", text)
        for command in scoped.usable:
            with self.subTest(command=command.path):
                self.assertIn(command.text, text)

    def test_the_whole_card_fits_discords_budget(self):
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        text = rendered_text(self.card(viewer))
        self.assertLessEqual(
            len(text), style.MAX_V2_CHARS,
            "Discord refuses a Components V2 message over its character budget, "
            "and a refused payload is a command that answers nothing",
        )

    def test_a_one_letter_search_matching_everything_still_fits(self):
        """The one input with no natural bound: what somebody typed.

        A search is the only page whose size is not the registry's, and a single
        letter matches nearly every row in the tree — including the parents of the
        yesterday-unrelated ones, through the notes. It is the worst query there
        is, and it has to fit, or the guard below is load-bearing rather than a
        safety net.
        """
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        text = rendered_text(self.card(viewer, search="a"))
        self.assertLessEqual(len(text), style.MAX_V2_CHARS)
        self.assertIn("/balance", text)

    def test_a_page_that_runs_out_of_room_says_how_many_it_left_out(self):
        """The guard itself, driven past its budget on purpose.

        A directory that stops at row ninety without saying so is worse than one
        that says "and twelve more": the reader cannot tell a short list from a
        truncated one. The budget is lowered here rather than waited for, because
        the day it fires in production is the day Discord would otherwise refuse
        the whole message.
        """
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        shown = help_cog._rows(self.rows, viewer, budget=120)
        self.assertGreater(len(self.rows), len(shown))
        trimmed = [line for line in shown if "more." in line]
        self.assertEqual(len(trimmed), 1, shown)
        left = int(re.search(r"and (\d+) more", trimmed[0]).group(1))
        self.assertEqual(left, len(self.rows) - (len(shown) - 1))

    def test_every_command_the_card_leaves_out_is_counted_and_explained(self):
        """Two numbers about one word are explained, never just printed."""
        # The server's own defaults, so the systems that arrive switched off are
        # off — which is the half of the line an administrator has to be able to
        # read off the card.
        viewer = viewer_for(discord.Permissions.none(), states={})
        scoped = directory.scope(self.rows, viewer)
        text = rendered_text(self.card(viewer))
        self.assertIn(f"{len(scoped.usable)} of {scoped.total}", text)
        for kind in scoped.order:
            self.assertIn(str(scoped.count(kind)), text)
        self.assertIn("switched off on this server", text)
        self.assertIn("Switched off here:", text)

    def test_a_row_the_picker_shows_more_widely_carries_its_caveat(self):
        """``/customquote delete`` is offered to everybody and works for one person."""
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        text = rendered_text(self.card(viewer, system="fun"))
        self.assertIn("/customquote delete", text)
        self.assertIn(directory.caveat(self.row("customquote delete"), viewer), text)

    def test_a_member_searching_for_a_withheld_command_is_told_why(self):
        """"Nothing found" would be a lie: ``/ban`` exists and needs a permission."""
        viewer = viewer_for(discord.Permissions.none())
        text = rendered_text(self.card(viewer, search="ban"))
        self.assertIn("No command you can run matches", text)
        self.assertIn("/ban", text)
        self.assertIn("needs **Ban Members**", text)

    def test_a_search_that_matches_nothing_says_so_plainly(self):
        viewer = viewer_for(discord.Permissions.none())
        text = rendered_text(self.card(viewer, search="zzzz"))
        self.assertIn("No command you can run matches", text)
        self.assertIn("Try a name", text)

    def test_a_search_names_the_matches_it_can_run_first(self):
        # The market is a *role* gate rather than a permission, so a viewer who
        # should match has to hold the role as well as the bit.
        viewer = viewer_for(
            discord.Permissions.all(), is_owner=True, roles=(7, market_role())
        )
        text = rendered_text(self.card(viewer, search="market buy"))
        self.assertIn("/market buy", text)
        self.assertIn("match **market buy**", text)
        self.assertNotIn("No command you can run matches", text)

    def test_a_withheld_row_is_named_beside_why_it_is_withheld(self):
        """Named, never silently dropped: an unlisted row reads as nonexistent.

        The card's whole job is answering *"does this exist, and may I run it"*,
        so a row this member cannot run is printed with the reason — which is the
        sentence they can act on: ask for the permission, or ask for the system
        to be switched on.
        """
        viewer = viewer_for(discord.Permissions.none(), states=ALL_ON)
        scoped = directory.scope(self.rows, viewer)
        withheld = [
            entry for entry in scoped.withheld
            if entry.reason.kind == directory.PERMISSION
            and entry.command.system_id == "system"
        ]
        self.assertTrue(withheld, "nothing on this card needs a permission, so this proves nothing")
        text = rendered_text(self.card(viewer, system="system"))
        for entry in withheld:
            with self.subTest(row=entry.command.text):
                self.assertIn(entry.command.text, text)
                self.assertIn(entry.reason.text, text)

    def test_the_operators_own_rows_are_counted_and_never_named(self):
        """The one silence on the card, and it is the rule ``permissions`` states.

        ``/qna inbox`` was the last row the operator kept to themselves, and it is
        gone with the system — so the law is pinned where it lives rather than
        through a registry that no longer has a row to exercise it. The two rows
        are written the way ``refusal`` writes them: a member reading a name they
        may not have is a leak, and a *count* beside the rows they can act on is
        not.
        """

        def row(path: str) -> directory.Command:
            return directory.Command(
                path=path, root=path.split()[0], system_id="system",
                system="System", icon="⚙️", note="", audience=permissions.DEV,
            )

        lines = help_cog._withheld_lines([
            directory.Held(
                row("bot db"),
                directory.Reason(directory.OPERATOR, "only the operator can run this"),
            ),
            directory.Held(
                row("ban"),
                directory.Reason(directory.PERMISSION, "needs **Ban Members**"),
            ),
        ])
        text = "\n".join(lines)
        self.assertNotIn("/bot db", text)
        self.assertIn("…and 1 only the operator can run.", text)
        # The public half is untouched: the count is an addition, not a curtain.
        self.assertIn("/ban", text)
        self.assertIn("needs **Ban Members**", text)

    def test_the_menu_offers_exactly_the_systems_this_member_can_run(self):
        viewer = viewer_for(discord.Permissions.none())
        scoped = directory.scope(self.rows, viewer)
        options = help_cog._cmd_options(scoped)
        values = [option.value for option in options]
        self.assertEqual(values[0], help_cog.CMD_ALL)
        expected = [group.id for group in directory.grouped(scoped.usable)]
        self.assertEqual(values[1:], expected)
        # A system this member has nothing in is left out: an option opening onto
        # "nothing here is yours" is a press that told them nothing.
        self.assertNotIn("market", values)

    def test_every_option_has_a_page_to_open(self):
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        scoped = directory.scope(self.rows, viewer)
        card = self.card(viewer)
        for option in help_cog._cmd_options(scoped):
            with self.subTest(value=option.value):
                self.assertIn(option.value, card.pages)

    def test_the_menu_stays_within_discords_option_cap(self):
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        options = help_cog._cmd_options(directory.scope(self.rows, viewer))
        self.assertLessEqual(len(options), 25)

    def test_the_menu_and_the_search_button_are_in_different_rows(self):
        """A row holds one select or up to five buttons, and Discord refuses both."""
        card = self.card(viewer_for(discord.Permissions.none()))
        rows = rows_of(card)
        self.assertEqual(len(rows), 2)
        for row in rows:
            items = list(row.children)
            selects = [item for item in items if isinstance(item, style.SELECT_TYPES)]
            with self.subTest(row=[type(item).__name__ for item in items]):
                if selects:
                    self.assertEqual(len(items), 1)
                else:
                    self.assertLessEqual(len(items), style.MAX_ROW_BUTTONS)

    def test_the_select_reaches_every_page_it_offers(self):
        """The menu edits the card in place, so each value has to resolve."""
        viewer = viewer_for(discord.Permissions.all(), is_owner=True)
        card = self.card(viewer)
        select = next(
            item
            for row in rows_of(card)
            for item in row.children
            if isinstance(item, style.SELECT_TYPES)
        )
        for option in select.options:
            with self.subTest(value=option.value):
                self.assertIn(option.value, card.pages)
                title, body = card.pages[option.value]
                self.assertTrue(title.startswith("## "))
                self.assertTrue(body)

    async def test_the_card_goes_to_the_person_who_asked(self):
        """It is about *them* — their permissions, their server's switches."""
        sent: list[dict] = []

        async def record(target, view, *, ephemeral=False, allowed_mentions=None):
            sent.append({"ephemeral": ephemeral, "view": view})

        class Response:
            def is_done(self):
                return False

            async def send_message(self, **_kwargs):
                return None

        interaction = FakeInteraction(discord.Permissions.none())
        interaction.client.tree = self.bot.tree
        interaction.response = Response()
        cog = help_cog.Help(self.bot)
        with mock.patch.object(help_cog, "send_panel", record):
            await help_cog.Help.cmd_cmd.callback(cog, interaction)
        self.assertEqual(len(sent), 1)
        self.assertTrue(
            sent[0]["ephemeral"],
            "a card about one member's permissions must not be posted to the channel",
        )

    async def test_a_dm_gets_no_directory(self):
        """There is no server to scope it to, and the card says which way out."""
        sent: list[dict] = []

        class Response:
            def is_done(self):
                return False

            async def send_message(self, content=None, **kwargs):
                sent.append({"content": content, **kwargs})

        interaction = FakeInteraction(discord.Permissions.none(), guild_id=None)
        interaction.guild = None
        interaction.client.tree = self.bot.tree
        interaction.response = Response()
        await help_cog.Help.cmd_cmd.callback(help_cog.Help(self.bot), interaction)
        self.assertEqual(len(sent), 1)
        self.assertIn("per server", sent[0]["content"])


if __name__ == "__main__":
    unittest.main()
