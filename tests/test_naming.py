"""Cards must never ping by accident, and must never print raw snowflakes.

Two real bugs are pinned here:

1. ``/richlist`` did ``guild.get_member(uid).display_name``. Without the Members
   intent that cache is always empty, so the list fell back to ``User 1234…``.
2. ``/leaderboard`` printed ``member.mention``. Legacy embeds never notified, but
   the Components V2 upgrade turns card text into ``TextDisplay`` — where a
   mention *does* notify — so simply asking for the rankings pinged everyone on
   the board. Cards now suppress mentions by default and lists use plain names.
"""
import unittest
from pathlib import Path

import discord

from utils import style

ROOT = Path(__file__).resolve().parent.parent


class _Member:
    def __init__(self, name: str):
        self.display_name = name
        self.name = name
        self.mention = "<@1>"


class _User:
    def __init__(self, name: str):
        self.display_name = name
        self.name = name


class _Guild:
    def __init__(self, member=None, fetched=None, boom: bool = False):
        self.id = 555
        self._member = member
        self._fetched = fetched
        self._boom = boom
        self.fetch_calls = 0

    def get_member(self, uid):
        return self._member

    async def fetch_member(self, uid):
        self.fetch_calls += 1
        if self._boom or self._fetched is None:
            raise RuntimeError("member unavailable")
        return self._fetched


class _Client:
    def __init__(self, user=None, boom: bool = False):
        self._user = user
        self._boom = boom
        self.calls = 0

    async def fetch_user(self, uid):
        self.calls += 1
        if self._boom or self._user is None:
            raise RuntimeError("user unavailable")
        return self._user


class TestDisplayNames(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        style.invalidate_name_cache()

    async def test_cached_member_needs_no_api_call(self):
        guild = _Guild(member=_Member("Nick"))
        self.assertEqual(await style.display_name(guild, 42), "Nick")
        self.assertEqual(guild.fetch_calls, 0, "must not hit the API when cached")

    async def test_missing_cache_falls_back_to_rest(self):
        guild = _Guild(fetched=_Member("Fetched"))
        self.assertEqual(await style.display_name(guild, 42), "Fetched")
        self.assertEqual(guild.fetch_calls, 1)

    async def test_rest_failure_falls_back_to_global_user(self):
        guild = _Guild(boom=True)
        client = _Client(user=_User("Global"))
        self.assertEqual(await style.display_name(guild, 42, client=client), "Global")

    async def test_total_failure_is_readable_not_a_mention(self):
        name = await style.display_name(_Guild(boom=True), 42, client=_Client(boom=True))
        self.assertEqual(name, "User 42")

    async def test_result_never_contains_mention_syntax(self):
        guild = _Guild(fetched=_Member("Safe"))
        for uid in (1, 2, 3):
            self.assertNotIn("<@", await style.display_name(guild, uid))

    async def test_results_are_cached(self):
        guild = _Guild(fetched=_Member("Once"))
        await style.display_name(guild, 99)
        await style.display_name(guild, 99)
        self.assertEqual(guild.fetch_calls, 1, "a repeat lookup must reuse the cache")

    async def test_batch_resolves_every_id(self):
        guild = _Guild(fetched=_Member("Batch"))
        names = await style.display_names(guild, [10, 11, 12])
        self.assertEqual(len(names), 3)
        self.assertTrue(all(isinstance(n, str) and n for n in names))

    async def test_zero_id_is_unknown(self):
        self.assertEqual(await style.display_name(_Guild(), 0), "Unknown")


class _Response:
    def __init__(self):
        self.sent = None
        self.edited = None

    def is_done(self):
        return False

    async def send_message(self, **kwargs):
        self.sent = kwargs
        return "sent"

    async def edit_message(self, **kwargs):
        self.edited = kwargs
        return "edited"


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = object()
        self.edit_original_response = object()


class _Channel:
    def __init__(self):
        self.kwargs = None

    async def send(self, **kwargs):
        self.kwargs = kwargs
        return "sent"


class TestCardMentionSafety(unittest.IsolatedAsyncioTestCase):
    """Components V2 mentions notify, so every card send defaults to silence."""

    async def test_send_card_suppresses_mentions_by_default(self):
        target = _Interaction()
        embed = discord.Embed(description="<@123> did a thing")
        await style.send_card(target, embed=embed)
        allowed = target.response.sent["allowed_mentions"].to_dict()
        self.assertEqual(allowed, {"parse": []})

    async def test_explicit_allowed_mentions_is_respected(self):
        target = _Interaction()
        ping = discord.AllowedMentions(users=True, roles=False, everyone=False)
        await style.send_card(target, embed=discord.Embed(title="x"), allowed_mentions=ping)
        self.assertIs(target.response.sent["allowed_mentions"], ping)

    async def test_edit_card_suppresses_mentions_by_default(self):
        target = _Interaction()
        await style.edit_card(target, embed=discord.Embed(title="x"))
        self.assertEqual(target.response.edited["allowed_mentions"].to_dict(), {"parse": []})

    async def test_editing_a_legacy_message_still_clears_content_and_embed(self):
        """Upgrading an embed to V2 must actively clear it, so the edit path
        cannot filter explicit ``None`` values out of the payload."""
        target = _Interaction()
        await style.edit_card(target, embed=discord.Embed(title="x"))
        payload = target.response.edited
        self.assertIn("content", payload)
        self.assertIsNone(payload["content"])
        self.assertIn("embed", payload)
        self.assertIsNone(payload["embed"])

    async def test_send_panel_suppresses_mentions_by_default(self):
        channel = _Channel()
        await style.send_panel(channel, style.card(title="t", body="<@123>"))
        self.assertEqual(channel.kwargs["allowed_mentions"].to_dict(), {"parse": []})


def _command_body(kind_markers, name: str, path: str) -> str:
    """Best-effort slice of one command's source, decorator through next command."""
    lines = (ROOT / path).read_text().splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if any(m in line for m in kind_markers) and f'name="{name}"' in line
    )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if any(m in lines[i] for m in kind_markers):
            end = i
            break
    return "\n".join(lines[start:end])


class _Intents:
    def __init__(self, members: bool):
        self.members = members


class _IntentsClient:
    def __init__(self, members: bool):
        self.intents = _Intents(members)


class TestMemberCacheAwareness(unittest.TestCase):
    """Counts derived from the member cache are only shown when it is real."""

    def test_false_when_members_intent_is_off(self):
        self.assertFalse(style.member_cache_complete(_IntentsClient(members=False)))

    def test_true_when_members_intent_is_on(self):
        self.assertTrue(style.member_cache_complete(_IntentsClient(members=True)))

    def test_false_for_an_object_without_intents(self):
        self.assertFalse(style.member_cache_complete(object()))

    def test_serverinfo_resolves_the_owner_without_the_cache(self):
        body = _command_body(['@app_commands.command'], "serverinfo", "cogs/server_control.py")
        self.assertIn("display_name(", body, "an owner must be named, not rendered as Unknown")
        self.assertNotIn("guild.owner.mention", body)

    def test_levelroles_gates_the_member_count(self):
        body = _command_body(['@app_commands.command'], "levelroles", "cogs/leveling.py")
        self.assertIn("member_cache_complete(", body)
        self.assertNotIn("*({members_count} members)*", body)


class TestEchoedTextCannotPing(unittest.TestCase):
    """User-supplied text must never let a member ping the server via the bot."""

    def test_uwu_suppresses_mentions(self):
        body = _command_body(['@app_commands.command'], "uwu", "cogs/social.py")
        self.assertIn("AllowedMentions.none()", body)

    def test_setlevel_public_confirmation_uses_a_plain_name(self):
        body = _command_body(['@app_commands.command'], "setlevel", "cogs/leveling.py")
        self.assertIn("**{member.display_name}**'s level", body)

    def test_givexp_public_confirmation_uses_a_plain_name(self):
        body = _command_body(['@app_commands.command'], "givexp", "cogs/leveling.py")
        self.assertIn("**{member.display_name}**! Current Level", body)

    def test_announce_deliberately_opts_in_to_pings(self):
        """The one place a ping is the whole point — admin-authored, Manage Server."""
        body = _command_body(['@app_commands.command'], "announce", "cogs/server_control.py")
        self.assertIn("allowed_mentions=", body)


class TestLeaderboardsUsePlainNames(unittest.TestCase):
    """Guard against a leaderboard reverting to a notifying mention."""

    def _assert_plain(self, body: str, label: str):
        self.assertIn("display_names(", body, f"{label} should resolve plain names")
        self.assertNotIn(".mention", body, f"{label} must not put a mention in a card")
        self.assertNotIn("<@", body, f"{label} must not build a mention by hand")

    def test_richlist_builds_plain_names(self):
        body = _command_body(
            ['@app_commands.command', '@eco_admin.command'],
            "richlist",
            "cogs/economy.py",
        )
        self._assert_plain(body, "/richlist")

    def test_leaderboard_builds_plain_names(self):
        body = _command_body(
            ['@app_commands.command', '@eco_admin.command'],
            "leaderboard",
            "cogs/leveling.py",
        )
        self._assert_plain(body, "/leaderboard")


if __name__ == "__main__":
    unittest.main()
