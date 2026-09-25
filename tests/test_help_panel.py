"""The ``/help`` panel is where an administrator reaches the dashboard now.

``/dashboard`` was a top-level command until the bot reached Discord's hundred-
command ceiling: a slot spent on an entry point that needs no slot at all, since
the panel administrators already open can hand out the same single-use link that
the command did — minted for the same member, scoped to the same server.

Two things keep that trade honest, and both are pinned here. The panel must
carry the button for an administrator and for nobody else, because the link is
the dashboard's only door when ``ADMIN_PASSKEY`` is unset. And its text must not
advertise commands that no longer exist — which is exactly how ``/meowfee``,
``/activity`` and ``/privacy`` stayed documented after they were folded into
their groups, and how a member ends up typing a command Discord has forgotten.
"""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

import keep_alive
from cogs import help as help_cog
from utils import features

ROOT = Path(__file__).resolve().parent.parent


class _Permissions:
    def __init__(self, administrator: bool):
        self.administrator = administrator


class _User:
    def __init__(self, administrator: bool):
        self.id = 4242
        self.display_name = "Admin"
        self.mention = f"<@{self.id}>"
        self.guild_permissions = _Permissions(administrator)
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")


class _Interaction:
    """The attributes minting a sign-in link reads, and nothing else."""

    def __init__(self, administrator: bool = True, guild_id: int | None = 555000):
        self.user = _User(administrator)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(name="Hmm", icon=None, banner=None)
        self.client = SimpleNamespace(user=_User(False))


def _link_buttons(view) -> list:
    """Every button carrying a URL, however deep the panel nests it."""
    found = []
    for container in view.children:
        for item in getattr(container, "children", []):
            for child in getattr(item, "children", []):
                if getattr(child, "url", None):
                    found.append(child)
    return found


class TestTheDashboardEntryPoint(unittest.TestCase):
    def test_an_administrator_gets_a_single_use_link(self):
        link = help_cog.dashboard_link(_Interaction(administrator=True))
        self.assertIsNotNone(link, "an administrator must be able to reach the dashboard")
        self.assertIn("/login?token=", link)

    def test_the_link_is_scoped_to_that_administrator_and_that_server(self):
        """The dashboard trusts the token's scope, so the token has to carry one."""
        link = help_cog.dashboard_link(_Interaction(administrator=True, guild_id=777))
        token = link.split("token=")[1]
        record = keep_alive.MAGIC_TOKENS[token]
        self.assertEqual((record["guild_id"], record["user_id"]), (777, 4242))
        self.assertGreater(record["expires_at"], 0)

    def test_a_non_administrator_gets_no_link(self):
        self.assertIsNone(help_cog.dashboard_link(_Interaction(administrator=False)))

    def test_a_dm_gets_no_link(self):
        """There is no guild to scope the session to, so there is no dashboard."""
        self.assertIsNone(help_cog.dashboard_link(_Interaction(guild_id=None)))

    def test_the_panel_carries_the_button_only_when_there_is_a_link(self):
        link = "https://dash.example/login?token=abc"
        with_link = help_cog.HelpLayout(thumbnail="https://cdn.example/x.png", dashboard=link)
        buttons = _link_buttons(with_link)
        self.assertEqual([b.url for b in buttons], [link])
        self.assertEqual([b.label for b in buttons], ["Open Dashboard"])

        plain = help_cog.HelpLayout(thumbnail="https://cdn.example/x.png")
        self.assertEqual(_link_buttons(plain), [], "a panel with no link must offer no link")


class _Response:
    """A response object that records how the panel was sent."""

    def __init__(self):
        self.sent = []

    def is_done(self):
        return False

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1)


class TestTheSignInLinkIsDeliveredPrivately(unittest.IsolatedAsyncioTestCase):
    """The link is a bearer credential for one server's dashboard.

    Nothing at redemption proves *who* is holding it — the token carries the
    server it was minted for and the administrator who asked — so the delivery is
    the security boundary. A panel posted into the channel with the button in it
    hands a live credential for that server's dashboard to everyone who can read
    the channel for the fifteen minutes the token lives, and the session it buys
    lasts eight hours and acts with the administrator's own live permissions.
    Pinned here rather than left to a helper's default argument, because that
    default is exactly what made the button public.
    """

    async def _send(self, *, administrator: bool) -> list[dict]:
        interaction = _Interaction(administrator=administrator)
        interaction.response = _Response()
        sent: list[dict] = []

        async def record(target, view, *, ephemeral=False, allowed_mentions=None):
            sent.append({"ephemeral": ephemeral, "view": view})

        with mock.patch.object(help_cog, "send_panel", record):
            await help_cog.Help.help_cmd.callback(help_cog.Help(None), interaction)
        return sent

    async def test_the_panel_carrying_a_link_goes_to_the_asker_alone(self):
        sent = await self._send(administrator=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            len(_link_buttons(sent[0]["view"])),
            1,
            "this panel is the one with the sign-in link in it",
        )
        self.assertTrue(
            sent[0]["ephemeral"],
            "a panel carrying a live sign-in link must not be posted to the channel",
        )

    async def test_a_member_who_gets_no_link_still_gets_the_shared_panel(self):
        sent = await self._send(administrator=False)
        self.assertEqual(len(sent), 1)
        self.assertEqual(_link_buttons(sent[0]["view"]), [])
        self.assertFalse(
            sent[0]["ephemeral"],
            "the panel everybody can use is not made private by this",
        )


class TestThePanelAdvertisesRealCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.failed: list[str] = []
        for cog in features.cog_paths():
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:  # pragma: no cover - reported as a failure
                self.failed.append(f"{cog}: {exc!r}")
        self.tree: dict[str, list[str] | None] = {
            c.name: (
                [s.name for s in c.commands]
                if isinstance(c, discord.app_commands.Group)
                else None
            )
            for c in self.bot.tree.get_commands()
        }

    async def asyncTearDown(self):
        try:
            await self.bot.close()
        except Exception:
            pass

    def test_every_cog_loads(self):
        self.assertEqual(self.failed, [])

    def test_the_dashboard_is_not_a_command(self):
        """The whole point of the fold: the panel replaces the slot, not the door."""
        self.assertNotIn("dashboard", self.tree)
        self.assertIn("help", self.tree)
        in_bot_py = re.findall(
            r'@bot\.tree\.command\(name="([^"]+)"', (ROOT / "bot.py").read_text()
        )
        self.assertNotIn("dashboard", in_bot_py)

    def test_every_command_the_panel_names_exists(self):
        """Text drift is silent: the member just gets \"unknown command\"."""
        missing: list[str] = []
        checked = 0
        for module in help_cog.HELP_MODULES.values():
            for match in re.finditer(r"`/([a-z0-9][a-z0-9-]*)(?:\s+([a-z0-9-]+))?", module["desc"]):
                root, sub = match.group(1), match.group(2)
                checked += 1
                if root not in self.tree:
                    missing.append(f"/{root}")
                elif sub and self.tree[root] is not None and sub not in self.tree[root]:
                    missing.append(f"/{root} {sub}")
        # A floor first: a regex that silently matched nothing would pass forever.
        self.assertGreater(checked, 50, "the panel's command references were not found")
        self.assertEqual(missing, [], f"the help panel names commands that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main()
