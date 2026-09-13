"""The bot has to register its own slash commands when it starts.

The only publishing path used to be the manual `/sync` slash command, and `/sync`
is itself a slash command — so a tree that had drifted could never repair itself.
That was the live state: 71 of 92 commands registered in the guild, with `/sync`
among the 21 missing, which is why nothing anyone ran could bring the new commands
back into view.

`bot.sync_commands` replaces that: sync every guild on boot, don't let one guild's
failure stop the others, respect a rate-limit backoff, and never raise into
`on_ready`.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

import bot as bot_module
from utils import commands_sync, features


class _FakeCommand:
    """A local command, as ``tree.get_commands()`` would hand it over."""

    def __init__(self, name: str):
        self.name = name

    def to_dict(self, _tree=None):
        return {"name": self.name, "description": "d"}


class _SyncError(RuntimeError):
    """Stands in for discord.RateLimited: a message and a retry_after."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class _FakeTree:
    def __init__(
        self,
        *,
        count: int = 92,
        fail_for=(),
        fail_times: int = 0,
        local_count: int = 3,
        up_to_date: bool = False,
        fetch_error: bool = False,
        fail_code: str | None = None,
        retry_after: float | None = None,
    ):
        self.calls: list = []
        self.count = count
        self.fail_for = set(fail_for)
        self.fail_times = fail_times
        self.fetch_error = fetch_error
        self.fail_code = fail_code
        self.retry_after = retry_after
        self.local = [_FakeCommand(f"cmd{index}") for index in range(local_count)]
        # An empty registry means "Discord has none of these", which is what most
        # of these cases want: a sync with real work to do. The registered side
        # holds command objects (like discord's AppCommand), not payloads, because
        # that is what fetch_commands returns.
        self.registered = [
            _FakeCommand(command.name) for command in self.local
        ] if up_to_date else []

    def get_commands(self):
        return self.local

    async def fetch_commands(self, *, guild=None):
        del guild
        if self.fetch_error:
            raise RuntimeError("cannot read")
        return self.registered

    async def sync(self, *, guild=None):
        name = getattr(guild, "name", None)
        self.calls.append(name)
        if name in self.fail_for:
            if self.fail_code:
                raise _SyncError(f"boom ({self.fail_code})", self.retry_after)
            raise RuntimeError("boom")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient")
        return [object()] * self.count


class _FakeBot:
    def __init__(self, guilds, tree):
        self.guilds = guilds
        self.tree = tree


def _guild(name: str):
    return SimpleNamespace(name=name, id=1)


class SyncCommands(unittest.IsolatedAsyncioTestCase):
    async def test_every_guild_is_synced_on_boot(self):
        tree = _FakeTree(count=92)
        fake = _FakeBot([_guild("Hmm"), _guild("Second Server")], tree)

        results = await bot_module.sync_commands(fake, retry_wait=0.01)

        self.assertEqual(tree.calls, ["Hmm", "Second Server"])
        self.assertEqual(results, {"Hmm": 92, "Second Server": 92})

    async def test_an_up_to_date_guild_is_left_alone(self):
        """Discord allows 200 writes a day per guild, so no writes when equal."""
        tree = _FakeTree(up_to_date=True)
        fake = _FakeBot([_guild("Hmm")], tree)

        results = await bot_module.sync_commands(fake, retry_wait=0.01)

        self.assertEqual(tree.calls, [], "an unchanged tree was re-uploaded")
        self.assertEqual(results, {"Hmm": 3})

    async def test_a_spent_write_budget_schedules_its_own_retry(self):
        """A deploy that lands while the window is closed must not give up."""
        tree = _FakeTree(fail_for={"Hmm"}, fail_code="30034", retry_after=430.0)
        fake = _FakeBot([_guild("Hmm")], tree)
        scheduled = []

        with mock.patch.object(
            bot_module, "_schedule_deferred_sync",
            new=lambda _bot, delay, remaining: scheduled.append((delay, remaining)),
        ):
            await bot_module.sync_commands(fake, attempts=2, retry_wait=0.01)

        self.assertEqual(scheduled, [(430.0, 1)], "no retry was queued for the spent budget")

    async def test_an_ordinary_failure_does_not_queue_a_retry(self):
        tree = _FakeTree(fail_for={"Hmm"})
        fake = _FakeBot([_guild("Hmm")], tree)
        scheduled = []

        with mock.patch.object(
            bot_module, "_schedule_deferred_sync",
            new=lambda _bot, delay, remaining: scheduled.append((delay, remaining)),
        ):
            await bot_module.sync_commands(fake, attempts=2, retry_wait=0.01)

        self.assertEqual(scheduled, [])

    async def test_the_last_deferred_retry_does_not_reschedule_forever(self):
        tree = _FakeTree(fail_for={"Hmm"}, fail_code="30034")
        fake = _FakeBot([_guild("Hmm")], tree)
        scheduled = []

        with mock.patch.object(
            bot_module, "_schedule_deferred_sync",
            new=lambda _bot, delay, remaining: scheduled.append((delay, remaining)),
        ):
            await bot_module.sync_commands(fake, attempts=1, deferred_retries=0)

        self.assertEqual(scheduled, [], "retries would never end")

    async def test_a_guild_whose_state_cannot_be_read_is_synced_anyway(self):
        tree = _FakeTree(fetch_error=True)
        fake = _FakeBot([_guild("Hmm")], tree)

        results = await bot_module.sync_commands(fake, retry_wait=0.01)

        self.assertEqual(tree.calls, ["Hmm"])
        self.assertEqual(results, {"Hmm": 92})

    async def test_a_broken_guild_does_not_stop_the_others(self):
        """One guild failing must not cost every other guild its commands."""
        tree = _FakeTree(fail_for={"Broken"})
        fake = _FakeBot([_guild("Broken"), _guild("Hmm")], tree)

        results = await bot_module.sync_commands(fake, attempts=3, retry_wait=0.01)

        self.assertEqual(results, {"Hmm": 92})
        self.assertEqual(tree.calls.count("Broken"), 3, "the failure was not retried")

    async def test_a_transient_failure_is_retried_and_succeeds(self):
        tree = _FakeTree(fail_times=1)
        fake = _FakeBot([_guild("Hmm")], tree)

        results = await bot_module.sync_commands(fake, attempts=3, retry_wait=0.01)

        self.assertEqual(results, {"Hmm": 92})
        self.assertEqual(tree.calls, ["Hmm", "Hmm"])

    async def test_a_failure_never_escapes_into_on_ready(self):
        """The bot must still come online when Discord refuses the sync."""
        tree = _FakeTree(fail_for={"Hmm"})
        fake = _FakeBot([_guild("Hmm")], tree)

        results = await bot_module.sync_commands(fake, attempts=2, retry_wait=0.01)

        self.assertEqual(results, {})

    async def test_no_guilds_is_not_an_error(self):
        results = await bot_module.sync_commands(
            _FakeBot([], _FakeTree()), retry_wait=0.01
        )

        self.assertEqual(results, {})


class CommandComparison(unittest.TestCase):
    """Only real changes may cost a write (Discord allows 200 per day, per guild)."""

    def test_identical_commands_need_no_write(self):
        local = [{"name": "loan", "description": "d",
                  "options": [{"name": "amount", "type": 4, "required": False}]}]
        remote = [{"id": "1", "application_id": "9", "guild_id": "5", "version": "7",
                   "type": 1, "name": "loan", "description": "d",
                   "options": [{"name": "amount", "type": 4, "required": False}]}]

        self.assertTrue(commands_sync.is_up_to_date(local, remote))

    def test_server_bookkeeping_keys_are_ignored(self):
        local = [{"name": "x", "description": "d"}]
        remote = [{"name": "x", "description": "d", "id": "1", "version": "2",
                   "name_localizations": None, "description_localizations": {},
                   "dm_permission": True, "default_member_permissions": None}]

        self.assertTrue(commands_sync.is_up_to_date(local, remote))

    def test_defaults_discord_omits_are_not_a_difference(self):
        """The exact payload discord.py sends for an unchanged `/help`.

        Discord drops `options: []`, `contexts: null`, `integration_types: null`
        and the deprecated `dm_permission` from its response. Treating those as a
        change made every command look stale, which would re-upload the whole tree
        on every boot and spend the daily write budget on nothing.
        """
        local = [{
            "name": "help", "description": "d", "options": [], "contexts": None,
            "integration_types": None, "dm_permission": True, "nsfw": False,
            "default_member_permissions": None,
        }]
        remote = [{
            "id": "1", "application_id": "9", "guild_id": "5", "version": "7", "type": 1,
            "name": "help", "description": "d", "nsfw": False,
            "default_member_permissions": None,
        }]

        self.assertTrue(commands_sync.is_up_to_date(local, remote))

    def test_removing_the_last_option_is_still_a_difference(self):
        """Empty-vs-absent must not swallow a real removal."""
        local = [{"name": "x", "description": "d", "options": []}]
        remote = [{"name": "x", "description": "d",
                   "options": [{"name": "gone", "type": 4}]}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_a_value_where_we_send_nothing_is_still_a_difference(self):
        local = [{"name": "x", "description": "d", "default_member_permissions": None}]
        remote = [{"name": "x", "description": "d", "default_member_permissions": "8"}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_a_changed_description_is_stale(self):
        local = [{"name": "loan", "description": "new"}]
        remote = [{"name": "loan", "description": "old", "id": "1"}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["loan"]))

    def test_a_new_option_is_stale(self):
        """This is exactly the case for /loan: same name, new `plan` option."""
        local = [{"name": "loan", "description": "d",
                  "options": [{"name": "plan", "type": 3}]}]
        remote = [{"name": "loan", "description": "d", "options": []}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["loan"]))

    def test_an_unregistered_command_is_missing(self):
        local = [{"name": "beg", "description": "d"}, {"name": "loan", "description": "d"}]
        remote = [{"name": "loan", "description": "d"}]

        self.assertEqual(commands_sync.diff(local, remote), (["beg"], []))

    def test_subcommand_order_is_significant(self):
        local = [{"name": "eco-admin", "description": "d", "options": [
            {"name": "add", "type": 1, "description": "a"},
            {"name": "remove", "type": 1, "description": "r"}]}]
        reordered = [{"name": "eco-admin", "description": "d", "options": [
            {"name": "remove", "type": 1, "description": "r"},
            {"name": "add", "type": 1, "description": "a"}]}]

        self.assertEqual(commands_sync.diff(local, reordered), ([], ["eco-admin"]))

    def test_the_summary_names_what_changed(self):
        local = [{"name": "beg", "description": "d"}, {"name": "loan", "description": "new"}]
        remote = [{"name": "loan", "description": "old"}]

        text = commands_sync.summary(local, remote)
        self.assertIn("beg", text)
        self.assertIn("loan", text)


class TheRealTreeRoundTrips(unittest.IsolatedAsyncioTestCase):
    async def test_discords_echo_of_our_payload_needs_no_rewrite(self):
        """If our payload didn't round-trip, the bot would re-upload every boot."""
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        for cog in features.cog_paths():
            await bot.load_extension(cog)
        try:
            local = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
            echo = [
                dict(payload, id=str(index), application_id="1", guild_id="2", version="3")
                for index, payload in enumerate(local)
            ]
            self.assertTrue(
                commands_sync.is_up_to_date(local, echo),
                f"our own payload would trigger a rewrite: "
                f"{commands_sync.summary(local, echo)}",
            )
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
