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
import asyncio
import contextlib
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
    """A local command, as ``tree.get_commands()`` would hand it over.

    ``command_id`` is what Discord attaches to a *registered* command — the
    handle a delete needs. It is optional so that the local commands, which
    never have one, stay exactly as they were.
    """

    def __init__(self, name: str, description: str = "d", command_id: str | None = None):
        self.name = name
        self.description = description
        self.id = command_id

    def to_dict(self, _tree=None):
        payload = {"name": self.name, "description": self.description}
        if self.id:
            payload["id"] = self.id
        return payload


class _FakeHTTP:
    def __init__(self, fail_on=None, retry_after=None, fail_code=None):
        self.sent: list = []
        self.global_sent: list = []
        self.deleted: list = []
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

    async def delete_guild_command(self, application_id, guild_id, command_id):
        del application_id, guild_id
        self.deleted.append(("guild", command_id))

    async def delete_global_command(self, application_id, command_id):
        del application_id
        self.deleted.append(("global", command_id))


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
            results, {"(global)": 0, "Hmm": 3, "Second Server": 3},
            "each server carries the tree now, and the shared scope holds only "
            "what has to work with no server to resolve it",
        )
        self.assertEqual(
            http.global_sent, [],
            "the tree was also pushed globally, which is what makes a per-server "
            "list impossible: a global command is in every server's picker",
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
            sorted(scheduled), [("Hmm", 430.0)],
            "the server with commands pending waits the window out; the shared "
            "lane published nothing, so it has nothing to wait for",
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


class PerServerCommandLists(unittest.IsolatedAsyncioTestCase):
    """Each server is registered with its own list, which is the whole feature.

    Discord cannot hide a globally-registered command in one server, so what a
    server's members see in their picker *is* what that server is registered
    with. These pin the three decisions that follow: what gets published, what
    gets taken away, and when the shared lane may be trimmed.
    """

    def _tree(self, *names):
        tree = _FakeTree(local_count=0)
        tree.local = [_FakeCommand(name) for name in names]
        return tree

    async def _sync(self, states, tree, guilds=None, http=None):
        http = http or _FakeHTTP()
        fake = _FakeBot(guilds or [_guild("Hmm")], tree, http)
        with mock.patch.object(
            bot_module.modules, "get_states", new=lambda _gid: dict(states)
        ):
            results = await bot_module.sync_commands(fake)
        return fake, http, results

    async def test_a_server_that_turned_a_module_off_is_not_registered_with_it(self):
        tree = self._tree("market", "balance", "help")
        _, http, _ = await self._sync({"market": False, "economy": True}, tree)
        self.assertEqual(http.sent, ["balance", "help"])

    async def test_a_server_that_kept_the_module_is_registered_with_it(self):
        tree = self._tree("market", "help")
        _, http, _ = await self._sync({"market": True}, tree)
        self.assertEqual(http.sent, ["market", "help"])

    async def test_a_core_command_is_published_however_the_modules_are_set(self):
        tree = self._tree("help", "dbstatus", "market")
        _, http, _ = await self._sync({"market": False}, tree)
        self.assertEqual(http.sent, ["help", "dbstatus"])

    async def test_a_module_switched_off_takes_its_command_out_of_this_server(self):
        tree = self._tree("market", "help")
        tree.registered = [
            _FakeCommand("market", command_id="id-market"),
            _FakeCommand("help", command_id="id-help"),
        ]
        _, http, _ = await self._sync({"market": False}, tree)
        self.assertEqual(
            http.deleted,
            [("guild", "id-market"), ("global", "id-market")],
            "the server's own copy goes, and so does the global one — a global "
            "command is in every server's picker, including the server that just "
            "turned the module off",
        )

    async def test_a_command_nobody_declared_is_never_taken_away(self):
        """The narrow rule is the whole safety of this: no name, no delete."""
        tree = self._tree("help")
        tree.registered = [_FakeCommand("mystery", command_id="id-mystery")]
        _, http, _ = await self._sync({}, tree)
        self.assertEqual(http.deleted, [])

    async def test_two_servers_get_their_own_lists_from_the_same_tree(self):
        tree = self._tree("market", "balance")
        states = {
            _guild("Hmm", 1).id: {"market": False, "economy": True},
            _guild("Second Server", 2).id: {"market": True, "economy": True},
        }
        http = _FakeHTTP()
        fake = _FakeBot([_guild("Hmm", 1), _guild("Second Server", 2)], tree, http)
        with mock.patch.object(
            bot_module.modules, "get_states", new=lambda gid: dict(states[gid])
        ):
            await bot_module.sync_commands(fake)
        # One request per command per server, in each server's own scope.
        self.assertEqual(http.sent, ["balance", "market", "balance"])

    async def test_the_shared_lane_is_trimmed_once_every_server_has_its_own(self):
        tree = self._tree("market", "help")
        tree.registered = [
            _FakeCommand("market", command_id="id-market"),
            _FakeCommand("help", command_id="id-help"),
        ]
        _, http, _ = await self._sync({"market": True}, tree)
        self.assertIn(
            ("global", "id-market"), http.deleted,
            "the global copy is in every server's picker, which is what a "
            "per-server list exists to prevent",
        )
        self.assertNotIn(
            ("global", "id-help"), http.deleted,
            "help has to work with no server to resolve it",
        )

    async def test_the_shared_lane_is_left_alone_when_a_server_is_incomplete(self):
        """A failed publish plus a successful delete would empty that server."""
        tree = self._tree("market", "help")
        tree.registered = [_FakeCommand("market", command_id="id-market")]
        http = _FakeHTTP(fail_on="help", fail_code="500")
        _, http, _ = await self._sync({"market": True}, tree, http=http)
        self.assertEqual(http.deleted, [], "a server was still missing commands")

    async def test_a_module_that_cannot_be_read_publishes_everything(self):
        """Failing open: an unreadable state can only leave a command visible."""
        tree = self._tree("market", "help")
        fake = _FakeBot([_guild("Hmm")], tree, _FakeHTTP())
        with mock.patch.object(
            bot_module.modules, "get_states", side_effect=RuntimeError("no database")
        ):
            await bot_module.sync_commands(fake)
        self.assertEqual(fake.http.sent, ["market", "help"])


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

        # Reported as counts rather than raising into on_ready: the one command
        # Discord accepted is counted, the refused one is not.
        self.assertEqual(results, {"(global)": 0, "Hmm": 1})

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


class RetiredCommandsAreDeleted(unittest.IsolatedAsyncioTestCase):
    """A removed feature must not keep failing in Discord's command picker.

    The publisher only ever upserts — that is what stops a spent write budget
    from clearing the guild — so a retired command has no local counterpart,
    which makes it neither missing nor stale: nothing would ever remove it.
    `/upload`, `/upload-big` and `/pin-upload-info` were retired with the upload
    feature on 2026-09-14, and the twelve folded ones (`/dashboard`, `/sync`,
    `/updatedb`, `/setavatar`, `/setbanner`, `/addlevelrole`,
    `/removelevelrole`, `/privacy`, `/tos`, `/meowfee`, `/activity-top`,
    `/activity-channel`) stayed in Discord's picker holding its 100 slots, which
    is what refused `/bot` and `/legal` with `30032`. Only names on that list may
    be deleted.
    """

    def _tree_with(self, *registered: str, with_id: bool = True):
        tree = _FakeTree(local_count=2)
        tree.registered = [
            _FakeCommand(name, command_id=f"id-{name}" if with_id else None)
            for name in ("cmd0", "cmd1", *registered)
        ]
        return tree

    async def test_a_retired_command_is_deleted(self):
        http = _FakeHTTP()

        await bot_module.sync_commands(_FakeBot([], self._tree_with("upload"), http))

        self.assertEqual(http.deleted, [("global", "id-upload")])

    async def test_every_retired_name_is_deleted(self):
        http = _FakeHTTP()

        await bot_module.sync_commands(
            _FakeBot([], self._tree_with("upload-big", "pin-upload-info"), http)
        )

        self.assertEqual(
            sorted(http.deleted), [("global", "id-pin-upload-info"), ("global", "id-upload-big")]
        )

    async def test_a_command_we_did_not_retire_is_never_deleted(self):
        """The whole point of the upsert publisher: nothing else may vanish."""
        http = _FakeHTTP()

        await bot_module.sync_commands(
            _FakeBot([], self._tree_with("some-other-bots-command"), http)
        )

        self.assertEqual(http.deleted, [])

    async def test_it_does_not_need_a_pending_change(self):
        """A tidy tree with one dead command must still clean it up."""
        http = _FakeHTTP()

        await bot_module.sync_commands(_FakeBot([], self._tree_with("upload"), http))

        self.assertEqual(http.sent + http.global_sent, [], "nothing needed republishing")
        self.assertEqual(http.deleted, [("global", "id-upload")])

    async def test_a_payload_with_no_id_is_skipped_rather_than_crashing(self):
        http = _FakeHTTP()

        await bot_module.sync_commands(
            _FakeBot([], self._tree_with("upload", with_id=False), http)
        )

        self.assertEqual(http.deleted, [])

    def test_the_real_retirement_list_names_every_command_we_folded_away(self):
        """These names have to be here, or they hold Discord's slots forever.

        Being on this list is the *only* way a command leaves Discord's picker:
        a retired name has no local counterpart, so it is neither missing nor
        stale, and nothing else would ever remove it.
        """
        self.assertEqual(
            commands_sync.RETIRED_COMMANDS,
            {
                "upload", "upload-big", "pin-upload-info",
                "dashboard", "setavatar", "setbanner", "sync", "updatedb",
                "addlevelrole", "removelevelrole", "privacy", "tos", "meowfee",
                "activity-top", "activity-channel",
            },
        )

    def test_a_slot_can_be_freed_so_a_new_command_fits(self):
        """The 100-command ceiling is why this list has to be complete.

        Discord answers ``30032 Maximum number of application commands reached``
        for a *new* name once the scope holds 100 chat-input commands, and the
        twelve folded commands were still holding slots — so `/bot` and `/legal`
        could not be created by any path. The delete of a retired name is what
        makes the create possible, and it happens before the upserts.
        """
        http = _FakeHTTP()

        async def run():
            await bot_module.sync_commands(
                _FakeBot([], self._tree_with("updatedb", "dashboard"), http)
            )

        asyncio.run(run())
        self.assertEqual(
            sorted(http.deleted),
            [("global", "id-dashboard"), ("global", "id-updatedb")],
        )

    async def test_no_retired_name_is_still_a_command_we_ship(self):
        """Retiring a name the tree still has would delete a live command.

        **Top-level names only.** Discord's 100-command ceiling and every delete
        here are about the top level, and a subcommand is free to share a label
        with a retired one: `/bot sync` is not `/sync`, and retiring the old
        top-level name must not be read as retiring the subcommand that replaced
        it. Walking the whole tree would fail on exactly that pair.
        """
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        try:
            for cog in features.cog_paths():
                await bot.load_extension(cog)
            live = {command.name for command in bot.tree.get_commands()}
        finally:
            await bot.close()

        self.assertEqual(commands_sync.RETIRED_COMMANDS & live, set())


class ARefusalCannotHideTheRest(unittest.IsolatedAsyncioTestCase):
    """One refused command used to stop the whole publish.

    ``_publish_scope`` returned on the first exception, so a single name Discord
    would not take — which is what a *new* command gets while a scope is at its
    100-command ceiling — left every update behind it unpublished, for weeks,
    with a summary that printed the count and not the reason.
    """

    def _tree(self, *names, registered=()):
        tree = _FakeTree(local_count=len(names), registered=list(registered) or None)
        tree.local = [_FakeCommand(name) for name in names]
        return tree

    def test_a_refused_command_does_not_stop_the_ones_behind_it(self):
        http = _FakeHTTP(fail_on="bot")
        tree = self._tree("bot", "alpha", "beta")

        status = asyncio.run(bot_module._sync_scope(_FakeBot([], tree, http)))

        self.assertEqual(status["published"], 2, "the two updates behind it went out")
        self.assertEqual(status["pending"], 1)
        self.assertEqual(
            sorted(http.global_sent), ["alpha", "beta", "bot"],
            "the ones behind the refusal were still attempted",
        )
        self.assertIn("bot", status["detail"], "and the refused one is named")

    def test_a_spent_write_budget_still_stops_immediately(self):
        """That window is closed for every command, so walking away is right."""
        http = _FakeHTTP(fail_on="bot", fail_code=30034, retry_after=900.0)
        tree = self._tree("bot", "alpha", "beta")

        status = asyncio.run(bot_module._sync_scope(_FakeBot([], tree, http)))

        self.assertEqual(status["published"], 0)
        self.assertEqual(
            http.global_sent, ["bot"],
            "nothing after a closed window may even be attempted",
        )
        self.assertEqual(status["retry_after"], 900.0, "and it says when to come back")


class TheStopIsExplained(unittest.IsolatedAsyncioTestCase):
    """A scope that published nothing has to say why — in the boot log."""

    def _run(self, status):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bot_module._explain_sync_stop("global", status)
        return out.getvalue()

    def test_a_full_scope_names_the_ceiling_and_the_cure(self):
        text = self._run(
            {"detail": "/bot: 400 Bad Request (error code: 30032): Maximum number of application commands reached"}
        )
        self.assertIn("30032", text)
        self.assertIn("100", text)
        self.assertIn("RETIRED_COMMANDS", text, "the cure, not just the cause")

    def test_an_ordinary_refusal_is_reported_verbatim(self):
        text = self._run({"detail": "/wat: 400 Invalid Form Body"})
        self.assertIn("Invalid Form Body", text)
        self.assertNotIn("RETIRED_COMMANDS", text, "no invented explanation")

    def test_a_budget_wait_is_not_explained_as_a_ceiling(self):
        text = self._run({"detail": "/bot: 429", "retry_after": 600.0})
        self.assertIn("/bot: 429", text)
        self.assertNotIn("ceiling", text)

    def test_a_scope_with_nothing_to_report_still_says_something(self):
        self.assertIn("no reason", self._run({}))


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

    def test_discords_string_encoding_is_not_a_change(self):
        """The eight permission-gated commands re-uploaded on every boot.

        discord.py sends `default_member_permissions: 8` and Discord stores and
        returns `"8"`. Comparing them directly made `/dashboard`, `/leaderboard`,
        `/addmoney`, `/removemoney`, `/setmoney`, `/addbank`, `/roulette` and
        `/rps` permanently "out of date" — captured from the live application.
        """
        local = [{"name": "addmoney", "description": "d", "default_member_permissions": 8,
                  "integration_types": None, "options": []}]
        remote = [{"id": "1", "application_id": "9", "version": "2", "type": 1,
                   "name": "addmoney", "description": "d",
                   "default_member_permissions": "8", "integration_types": [0, 1],
                   "nsfw": False}]

        self.assertTrue(commands_sync.is_up_to_date(local, remote))

    def test_a_stripped_variation_selector_is_not_a_change(self):
        """Discord drops U+FE0F, so `/rps` looked stale on every boot.

        Captured from the live application: we send `"✂️ Scissors"` (U+2702
        U+FE0F) and Discord returns `"✂ Scissors"` (U+2702).
        """
        local = [{"name": "rps", "description": "d", "options": [
            {"type": 3, "name": "choice", "description": "c", "choices": [
                {"name": "✂️ Scissors", "value": "scissors"}]}]}]
        remote = [{"id": "1", "type": 1, "name": "rps", "description": "d", "options": [
            {"type": 3, "name": "choice", "description": "c", "choices": [
                {"name": "✂ Scissors", "value": "scissors"}]}]}]

        self.assertTrue(commands_sync.is_up_to_date(local, remote))

    def test_a_real_emoji_change_is_still_a_change(self):
        local = [{"name": "x", "description": "d", "options": [
            {"choices": [{"name": "✂️ Scissors", "value": "scissors"}]}]}]
        remote = [{"name": "x", "description": "d", "options": [
            {"choices": [{"name": "🪨 Rock", "value": "rock"}]}]}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_a_genuinely_different_permission_bitfield_is_still_a_change(self):
        local = [{"name": "x", "description": "d", "default_member_permissions": 8}]
        remote = [{"name": "x", "description": "d", "default_member_permissions": "1024"}]

        self.assertEqual(commands_sync.diff(local, remote), ([], ["x"]))

    def test_a_boolean_is_never_coerced_to_a_string(self):
        """`True == 1` in Python, so booleans need their own guard."""
        self.assertFalse(commands_sync.matches(True, "1"))
        self.assertFalse(commands_sync.matches(False, "0"))

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


class ASwitchBurstCostsOnePublish(unittest.IsolatedAsyncioTestCase):
    """Switching modules off is one command-set change, not one per click.

    The dashboard's "disable all" flips eleven modules back to back, and each
    flip asks the bot to republish that server's commands. Eleven publishes of
    ninety commands each is how a server's daily write budget disappears, so a
    burst is coalesced: the pass waits for the clicking to stop, then reads the
    state the clicking left behind. Nothing else in this file would notice if
    that coalescing broke, which is the point of pinning it here.
    """

    GUILD_ID = 4242

    def setUp(self):
        self.guild = SimpleNamespace(id=self.GUILD_ID, name="Development")
        self.bot = SimpleNamespace(guilds=[self.guild])
        self.publishes: list[dict] = []
        self.state = {"market": True, "economy": True}
        self.in_flight = None
        self.release = None
        self.hold_once = False
        bot_module._resync_dirty.clear()
        bot_module._resync_tasks.clear()

    def tearDown(self):
        bot_module._resync_dirty.clear()
        for task in list(bot_module._resync_tasks.values()):
            task.cancel()
        bot_module._resync_tasks.clear()

    async def _fake_sync(self, _bot, _guild, *, force=False):
        """Stands in for the publisher, recording the state it would send."""
        del force
        self.publishes.append(dict(self.state))
        if self.hold_once:
            self.hold_once = False
            self.in_flight.set()
            await self.release.wait()
        return {
            "registered": len(self.state), "pending": 0, "published": 0,
            "retry_after": None, "summary": "up to date", "detail": "",
        }

    def _patched(self):
        return (
            mock.patch.object(bot_module, "RESYNC_SETTLE_SECONDS", 0.3),
            mock.patch.object(bot_module, "_sync_guild", new=self._fake_sync),
        )

    async def test_eleven_switches_clicked_fast_are_one_publish(self):
        settle, sync = self._patched()
        with settle, sync:
            for _ in range(11):
                await bot_module._kick_guild_resync(self.bot, self.guild)
                await asyncio.sleep(0.01)  # a person clicking, not a tight loop
            self.state = {"market": False, "economy": False}
            await asyncio.sleep(0.5)

        self.assertEqual(
            len(self.publishes), 1,
            f"eleven clicks cost {len(self.publishes)} publishes of the whole tree",
        )
        self.assertEqual(
            self.publishes[0], {"market": False, "economy": False},
            "the single pass published the state from before the clicking stopped",
        )
        self.assertFalse(bot_module._resync_dirty, "the server was left queued")
        self.assertFalse(bot_module._resync_tasks, "the pass left itself behind")

    async def test_a_switch_landing_during_the_publish_gets_its_own_pass(self):
        """The case the loop exists for: the publish is already running."""
        self.in_flight, self.release = asyncio.Event(), asyncio.Event()
        self.hold_once = True
        settle, sync = self._patched()
        with settle, sync:
            await bot_module._kick_guild_resync(self.bot, self.guild)
            await asyncio.wait_for(self.in_flight.wait(), timeout=5)
            self.state = {"market": False, "economy": True}
            await bot_module._kick_guild_resync(self.bot, self.guild)
            self.release.set()
            await asyncio.sleep(0.6)

        self.assertEqual(
            len(self.publishes), 2,
            "a switch that landed mid-publish was never published",
        )
        self.assertEqual(self.publishes[0], {"market": True, "economy": True})
        self.assertEqual(self.publishes[1], {"market": False, "economy": True})
        self.assertFalse(bot_module._resync_dirty)
        self.assertFalse(bot_module._resync_tasks)

    async def test_a_second_click_does_not_start_a_second_pass(self):
        settle, sync = self._patched()
        with settle, sync:
            await bot_module._kick_guild_resync(self.bot, self.guild)
            queued = bot_module._resync_tasks[self.GUILD_ID]
            for _ in range(5):
                await bot_module._kick_guild_resync(self.bot, self.guild)
            self.assertIs(
                bot_module._resync_tasks[self.GUILD_ID], queued,
                "a click while one was already queued started another pass",
            )
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.publishes), 1)


class TheResyncTriggerIsHonestAboutFailing(unittest.TestCase):
    """A request for a republish that cannot happen says so rather than lying."""

    def setUp(self):
        self.client = mock.Mock()

    def test_a_server_the_bot_is_not_in_is_refused(self):
        self.client.get_guild.return_value = None
        with mock.patch.object(bot_module, "bot", self.client):
            self.assertFalse(bot_module.resync_guild_commands(1234))

    def test_a_bot_that_is_not_running_is_refused(self):
        self.client.get_guild.return_value = SimpleNamespace(id=1, name="x")
        self.client.loop = None
        with mock.patch.object(bot_module, "bot", self.client):
            self.assertFalse(bot_module.resync_guild_commands(1))

    def test_no_server_named_is_refused(self):
        with mock.patch.object(bot_module, "bot", self.client):
            self.assertFalse(bot_module.resync_guild_commands(0))


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
