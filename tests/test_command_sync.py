"""The bot has to publish its own slash commands — safely.

Two failures are pinned here.

1. **Nothing published the tree.** The only path was the manual `/sync` command,
   which is itself a slash command, so a drifted tree could never repair itself.
   The live guild ran 71 of 93 commands with `/sync` among the missing 22.

2. **The bulk overwrite is destructive.** Discord rebuilds the guild's command
   list on a bulk sync, so when the daily write budget ran out part-way through
   (error 30034) the deletes had already landed: the guild went from 74 commands
   to one. The sync now upserts each missing/changed command individually, which
   can only ever add or replace.
"""
import io
import tokenize
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

import bot as bot_module
from utils import commands_sync, features


class _SyncError(RuntimeError):
    """Stands in for discord.RateLimited: a message and a retry_after."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class _FakeCommand:
    """A local command, as ``tree.get_commands()`` would hand it over."""

    def __init__(self, name: str, description: str = "d"):
        self.name = name
        self.description = description

    def to_dict(self, _tree=None):
        return {"name": self.name, "description": self.description}


class _FakeHTTP:
    def __init__(self, fail_on=None, retry_after=None, fail_code=None):
        self.sent: list = []
        self.global_sent: list = []
        self.fail_on = fail_on
        self.retry_after = retry_after
        self.fail_code = fail_code

    def _record(self, bucket, payload):
        bucket.append(payload["name"])
        if payload["name"] == self.fail_on:
            raise _SyncError(f"boom ({self.fail_code})", self.retry_after)
        return {"id": "1"}

    async def upsert_guild_command(self, application_id, guild_id, payload):
        del application_id, guild_id
        return self._record(self.sent, payload)

    async def upsert_global_command(self, application_id, payload):
        del application_id
        return self._record(self.global_sent, payload)


class _FakeTree:
    def __init__(self, *, local_count: int = 3, registered=None, fetch_error: bool = False):
        self.fetch_error = fetch_error
        self.local = [_FakeCommand(f"cmd{index}") for index in range(local_count)]
        self.registered = registered or []

    def get_commands(self):
        return self.local

    async def fetch_commands(self, *, guild=None):
        del guild
        if self.fetch_error:
            raise RuntimeError("cannot read")
        return self.registered

    async def sync(self, *, guild=None):  # pragma: no cover - must never be called
        raise AssertionError(
            "the destructive bulk sync was used; a rejected bulk sync deletes the "
            "guild's whole command list"
        )


class _FakeBot:
    def __init__(self, guilds, tree, http=None, application_id=999):
        self.guilds = guilds
        self.tree = tree
        self.http = http or _FakeHTTP()
        self.application_id = application_id


def _guild(name: str, gid: int = 1):
    return SimpleNamespace(name=name, id=gid)


def _payloads(*names: str) -> list[dict]:
    return [{"name": name, "description": "d"} for name in names]


class SyncCommands(unittest.IsolatedAsyncioTestCase):
    async def test_every_guild_gets_its_commands(self):
        http = _FakeHTTP()
        fake = _FakeBot([_guild("Hmm"), _guild("Second Server", 2)], _FakeTree(), http)

        results = await bot_module.sync_commands(fake)

        self.assertEqual(http.sent, ["cmd0", "cmd1", "cmd2"] * 2)
        self.assertEqual(
            results, {"(global)": 3, "Hmm": 3, "Second Server": 3},
            "the global scope is a second lane with its own write budget",
        )

    async def test_only_missing_or_changed_commands_are_sent(self):
        """Discord allows 200 writes a day per guild, so unchanged ones are skipped."""
        tree = _FakeTree(local_count=3)
        tree.registered = [_FakeCommand("cmd0"), _FakeCommand("cmd1")]
        http = _FakeHTTP()

        await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        self.assertEqual(http.sent, ["cmd2"], "an unchanged command was re-uploaded")

    async def test_a_changed_command_is_sent(self):
        tree = _FakeTree(local_count=1)
        tree.local = [_FakeCommand("loan", "new description")]
        tree.registered = [_FakeCommand("loan", "old description")]
        http = _FakeHTTP()

        await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        self.assertEqual(http.sent, ["loan"])

    async def test_an_up_to_date_guild_costs_nothing(self):
        tree = _FakeTree(local_count=3)
        tree.registered = [_FakeCommand(command.name) for command in tree.local]
        http = _FakeHTTP()

        results = await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        self.assertEqual(http.sent, [], "an unchanged tree was re-uploaded")
        self.assertEqual(http.global_sent, [], "an unchanged global tree was re-uploaded")
        self.assertEqual(results, {"(global)": 3, "Hmm": 3})

    async def test_a_guild_whose_state_cannot_be_read_is_published_anyway(self):
        tree = _FakeTree(local_count=2, fetch_error=True)
        http = _FakeHTTP()

        await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        self.assertEqual(http.sent, ["cmd0", "cmd1"])

    async def test_a_spent_write_budget_schedules_the_background_drain(self):
        """A deploy that lands while the window is closed must not give up."""
        tree = _FakeTree(local_count=3)
        http = _FakeHTTP(fail_on="cmd1", fail_code="30034", retry_after=430.0)
        scheduled = []
        guild = _guild("Hmm")

        with mock.patch.object(
            bot_module, "_schedule_command_drain",
            new=lambda _bot, _guild, delay, **_kwargs: scheduled.append(
                (getattr(_guild, "name", "global"), delay)
            ),
        ):
            await bot_module.sync_commands(_FakeBot([guild], tree, http))

        self.assertEqual(http.sent, ["cmd0", "cmd1"], "it kept going after the limit")
        self.assertEqual(
            sorted(scheduled), [("Hmm", 430.0), ("global", 430.0)],
            "both lanes wait out the window",
        )

    async def test_an_ordinary_failure_does_not_queue_a_drain(self):
        tree = _FakeTree(local_count=2)
        http = _FakeHTTP(fail_on="cmd1", fail_code="500", retry_after=5.0)
        scheduled = []

        with mock.patch.object(
            bot_module, "_schedule_command_drain",
            new=lambda _bot, _guild, delay, **_kwargs: scheduled.append(delay),
        ):
            await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        self.assertEqual(scheduled, [], "an ordinary error is not a budget problem")

    async def test_a_complete_guild_is_not_drained(self):
        tree = _FakeTree(local_count=3)
        tree.registered = [_FakeCommand(command.name) for command in tree.local]
        scheduled = []

        with mock.patch.object(
            bot_module, "_schedule_command_drain",
            new=lambda _bot, _guild, delay, **_kwargs: scheduled.append(delay),
        ):
            await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, _FakeHTTP()))

        self.assertEqual(scheduled, [])


class DrainingPendingCommands(unittest.IsolatedAsyncioTestCase):
    """The budget refills a slot at a time, so the drain keeps trying."""

    def setUp(self):
        self.slept = []

    async def _drain(self, statuses, *, budget=None):
        async def fake_sleep(seconds):
            self.slept.append(seconds)

        calls = list(statuses)

        async def fake_sync(_bot, _guild, **_kwargs):
            return calls.pop(0) if calls else {"pending": 0, "registered": 0, "retry_after": None}

        for patch in (
            mock.patch.object(bot_module, "_sleep", new=fake_sleep),
            mock.patch.object(bot_module, "_sync_guild", new=fake_sync),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        kwargs = {"budget_seconds": budget} if budget is not None else {}
        return await bot_module.drain_pending(
            _FakeBot([], _FakeTree()), _guild("Hmm"), first_delay=430.0, **kwargs
        )

    async def test_it_stops_once_everything_is_published(self):
        done = await self._drain([
            {"pending": 5, "registered": 88, "retry_after": 300.0},
            {"pending": 0, "registered": 93, "retry_after": None},
        ])

        self.assertTrue(done)
        self.assertEqual(len(self.slept), 2)
        self.assertEqual(self.slept[0], 430.0, "the reported window was not honoured")
        self.assertEqual(self.slept[1], 300.0)

    async def test_it_gives_up_when_the_time_budget_runs_out(self):
        done = await self._drain(
            [{"pending": 5, "registered": 88, "retry_after": 300.0}], budget=0.0
        )

        self.assertFalse(done, "the drain would never end")

    async def test_the_wait_between_cycles_is_bounded(self):
        await self._drain(
            [{"pending": 5, "registered": 88, "retry_after": 99_999.0},
             {"pending": 0, "registered": 93, "retry_after": None}],
        )

        self.assertLessEqual(self.slept[0], bot_module.DRAIN_MAX_WAIT)

    async def test_a_failure_never_escapes_into_on_ready(self):
        tree = _FakeTree(local_count=2, fetch_error=True)
        http = _FakeHTTP(fail_on="cmd0", fail_code="500")

        results = await bot_module.sync_commands(_FakeBot([_guild("Hmm")], tree, http))

        # Reported as a count of zero rather than raising into on_ready.
        self.assertEqual(results, {"(global)": 0, "Hmm": 0})

    async def test_no_guilds_still_publishes_the_global_scope(self):
        """A bot in no guilds can still be found by the next invitation."""
        results = await bot_module.sync_commands(_FakeBot([], _FakeTree()))

        self.assertEqual(results, {"(global)": 3})


class RetryDelay(unittest.TestCase):
    """Only the spent write budget is worth waiting out."""

    def test_the_daily_limit_uses_the_reported_window(self):
        self.assertEqual(bot_module._retry_delay(_SyncError("boom (30034)", 430.0)), 430.0)

    def test_the_daily_limit_falls_back_when_no_window_is_reported(self):
        self.assertEqual(bot_module._retry_delay(_SyncError("boom (30034)")), 600.0)

    def test_the_wait_is_clamped(self):
        self.assertEqual(bot_module._retry_delay(_SyncError("boom (30034)", 99999.0)), 1800.0)
        self.assertEqual(bot_module._retry_delay(_SyncError("boom (30034)", 1.0)), 60.0)

    def test_discords_error_code_is_recognised(self):
        class _WithCode(_SyncError):
            code = 30034

        self.assertEqual(bot_module._retry_delay(_WithCode("whatever")), 600.0)

    def test_other_failures_are_not_retried(self):
        for exc in (_SyncError("boom"), _SyncError("boom (500)", 5.0)):
            self.assertIsNone(bot_module._retry_delay(exc))


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

        Discord drops `options: []`, `contexts: null`, `integration_types: null`,
        `required: false` and the deprecated `dm_permission` from its response.
        Treating those as a change made every command look stale, which would
        re-upload the whole tree on every boot — verified against the live guild,
        where 73 of 74 registered commands appeared "changed".
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

    def test_removing_the_last_option_is_still_a_difference(self):
        local = [{"name": "x", "description": "d", "options": []}]
        remote = [{"name": "x", "description": "d",
                   "options": [{"name": "gone", "type": 4}]}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_a_value_where_we_send_nothing_is_still_a_difference(self):
        local = [{"name": "x", "description": "d", "default_member_permissions": None}]
        remote = [{"name": "x", "description": "d", "default_member_permissions": "8"}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_an_unregistered_command_is_missing(self):
        local = _payloads("beg", "loan")
        remote = _payloads("loan")

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


class TheSyncCommandIsNotDestructive(unittest.TestCase):
    """`/sync` was a second route to the wipe the boot sync had already had.

    It called `tree.sync(guild=...)` and then `tree.sync()` — both bulk
    overwrites, which Discord applies by clearing the scope's command list and
    rebuilding it, so a run that meets the daily write limit leaves the guild
    with almost nothing. The command now goes through the upsert publisher.
    """

    def _sync_body(self) -> str:
        """The command's actual code, with comments stripped.

        Comments are removed first: the explanation of *why* the bulk sync is
        forbidden names it, and a substring check on raw source would flag that.
        """
        source = (
            Path(__file__).resolve().parent.parent / "cogs" / "bot_config.py"
        ).read_text(encoding="utf-8")
        code = tokenize.untokenize([
            token for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type != tokenize.COMMENT
        ])
        return code.split("async def sync(self", 1)[1].split("async def ", 1)[0]

    def test_it_never_bulk_overwrites(self):
        body = self._sync_body()

        self.assertNotIn("tree.sync(", body, "the destructive bulk sync is back in /sync")
        self.assertNotIn("copy_global_to", body)

    def test_it_uses_the_safe_publisher(self):
        self.assertIn("publish_commands", self._sync_body())

    def test_the_bot_exposes_the_publisher_to_cogs(self):
        """Cogs must not import bot: it runs as `__main__`, so that re-executes it."""
        source = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")

        self.assertIn("bot.publish_commands = sync_commands", source)


if __name__ == "__main__":
    unittest.main()
