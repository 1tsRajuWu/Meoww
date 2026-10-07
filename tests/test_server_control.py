"""Whole-server actions: the plan, the ledger, and the undo that is the server's.

A lockdown is a bulk permission change over somebody else's server, so the tests
are mostly about the two halves that make it safe to offer from a web page: it
writes down what it changed **before** it changes anything, and unlocking puts
back what was there rather than allowing `@everyone` to talk again — which on a
server that had deliberately locked a channel would be handing out a permission
nobody asked for.
"""
import contextlib
import datetime
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import TempDatabaseMixin
from utils import db, moderation_actions, modlog, server_control

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Perms:
    def __init__(self, *, manage_channels=True, administrator=False, **rest):
        self.manage_channels = manage_channels
        self.administrator = administrator
        for name, value in rest.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        return False


class Bits:
    """A bitfield that answers the three questions a lockdown asks.

    It has to answer them *properly*: a ``Mock`` is truthy for every attribute,
    so a fake whose deny answered ``Mock`` made every channel look already locked
    and the lockdown a no-op — the sort of fake that turns a test into a
    statement about itself.
    """

    #: Discord's own bit for each permission the lockdown engine reads — and the
    #: **docs'** numbering rather than a remembered one: `create_public_threads` sat
    #: at ``1 << 34`` here, which is Discord's MANAGE_THREADS. Nothing caught it while
    #: every law compared these bits to each other, and it stopped being harmless the
    #: moment the engine compared a deny against Discord's own mask: every real lock
    #: in this file would have been judged somebody else's mask. Cross-checked against
    #: the published table (`CREATE_PUBLIC_THREADS 0x0000000800000000`, `1 << 35`) and
    #: against what the live bot writes — the deny Discord holds for a `/lockdown` is
    #: `0x800000840`.
    NAMES = {
        "view_channel": 1 << 10,
        "send_messages": 1 << 11,
        "create_public_threads": 1 << 35,
        "add_reactions": 1 << 6,
    }

    def __init__(self, value=0):
        self.value = int(value)

    def __getattr__(self, name):
        bit = self.NAMES.get(name)
        if bit is None:
            return False
        return bool(self.value & bit)


#: Everything a lockdown denies, as one mask — what "already locked" looks like.
ALL_LOCKED = Bits.NAMES["send_messages"] | Bits.NAMES["create_public_threads"] | Bits.NAMES["add_reactions"]


class Overwrite:
    """A permission overwrite, **shaped like Discord's own object**.

    ``pair()`` and not ``allow``/``deny`` attributes, and that is the point:
    `discord.PermissionOverwrite` has no ``allow`` attribute at all, so a stand-in
    that offered one let the engine read the wrong attribute and pass here while
    doing nothing whatever on Discord — which it did, until a read-back of the
    real object caught it. Now this stand-in would fail the same way the real one
    did, and loudly.
    """

    def __init__(self, *, allow=0, deny=0):
        self._allow = Bits(allow)
        self._deny = Bits(deny)

    def pair(self):
        return self._allow, self._deny


class Role:
    def __init__(self, name="role", position=0, *, id=1, manage_channels=False):
        self.name = name
        self.position = position
        self.id = id
        # What the role grants server-wide. Only a *hide* reads it: it is how the
        # people who could open the channel again are told from everybody else.
        self.permissions = SimpleNamespace(manage_channels=manage_channels)

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)


class Channel:
    def __init__(self, channel_id, name="general", *, kind="text", manage=True, overwrites=None):
        self.id = channel_id
        self.name = name
        self.type = kind
        self.overwrites = dict(overwrites or {})
        self.guild = None
        self._manage = manage
        self.calls = []
        self.slowmode_delay = 0

    def permissions_for(self, _member):
        return Perms(manage_channels=self._manage)

    async def set_permissions(self, target, *, overwrite=None, reason=""):
        self.calls.append(("set_permissions", target, overwrite, reason))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def edit(self, *, slowmode_delay=0, reason=""):
        self.calls.append(("edit", slowmode_delay, reason))
        self.slowmode_delay = slowmode_delay


class Guild:
    def __init__(self, guild_id=424242, *, channels=(), audit_logs=None, roles=(), members=()):
        self.id = guild_id
        self.name = "Test Server"
        self.default_role = Role("@everyone")
        # The server-wide floor, and the one permission of it `@everyone`'s leftover
        # overwrites are read against: a stand-in that granted nothing would make
        # every removal look like it took Send Messages away, which is the opposite
        # of what these laws are about.
        self.default_role.permissions.send_messages = True
        # My own member, under the id the bot's user carries: a lock never touches
        # it, so a fake where it collided with `@everyone` could not tell.
        self.me = Role("Meow", id=424242)
        self.channels = list(channels)
        self.roles = [self.default_role, self.me, *roles]
        self.members = list(members)
        self.gone = set()
        self._audit = audit_logs
        for channel in self.channels:
            channel.guild = self

    def get_channel(self, channel_id):
        if channel_id in self.gone:
            return None
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_role(self, role_id):
        for role in [self.default_role, *self.roles]:
            if int(getattr(role, "id", 0) or 0) == int(role_id):
                return role
        return None

    def get_member(self, member_id):
        for member in self.members:
            if int(getattr(member, "id", 0) or 0) == int(member_id):
                return member
        return None

    def audit_logs(self, **kwargs):
        """Discord's audit log, or what a stand-in server without one answers.

        The real read is what tells this bot's own lock from an administrator's,
        so a fake that answered an empty page by default would make every
        adoption law pass for the wrong reason. No log means no evidence.
        """
        if self._audit is None:
            raise RuntimeError("this stand-in server has no audit log")
        return self._audit(**kwargs)


class Actor:
    id = 5
    label = "Admin (website)"


class TheHide(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """`/hide`: a room off everybody's list, with the same ledger underneath.

    Three facts have to hold together, and each is a way this is quietly wrong:
    the deny has to reach the **leaks** (an allow on a role beats a deny on
    `@everyone`, which is the defect the lock engine was built around), the row it
    writes has to be a real **undo** rather than an allow nobody granted, and the
    row must **not** be a lock — the message sweep reads the same table, and a
    hidden room read as a locked one would have everything posted in it deleted
    for as long as nobody could see it.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _server(self, *channels, roles=()):
        guild = Guild(channels=list(channels), roles=list(roles))
        for channel in channels:
            channel.guild = guild
        return guild

    async def test_it_denies_view_channel_and_nothing_else(self):
        """A hidden room is not a quiet one: `/hide` writes one bit."""
        everyone = Role("@everyone")
        channel = Channel(1, "secrets")
        guild = self._server(channel)
        guild.default_role = everyone
        report = await server_control.hide_channels(guild, [channel], Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.changed, 1)
        written = channel.overwrites[everyone]
        self.assertIs(written.view_channel, False)
        self.assertIsNot(
            written.send_messages, False, "a hide started deleting what people write"
        )

    async def test_a_role_that_was_let_in_loses_its_sight_too(self):
        """An allow on a role is applied after `@everyone`'s deny, so it wins."""
        everyone = Role("@everyone")
        onlookers = Role("Onlookers", id=9)
        channel = Channel(
            1,
            "secrets",
            overwrites={onlookers: Overwrite(allow=Bits.NAMES["view_channel"])},
        )
        guild = self._server(channel, roles=[onlookers])
        guild.default_role = everyone
        report = await server_control.hide_channels(guild, [channel], Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertIs(channel.overwrites[everyone].view_channel, False)
        self.assertIs(
            channel.overwrites[onlookers].view_channel,
            False,
            "the room stayed on the list of a role that had been let in",
        )

    async def test_showing_puts_back_the_overwrite_that_was_there(self):
        everyone = Role("@everyone")
        channel = Channel(
            1,
            "secrets",
            overwrites={everyone: Overwrite(allow=Bits.NAMES["view_channel"])},
        )
        guild = self._server(channel)
        guild.default_role = everyone
        await server_control.hide_channels(guild, [channel], Actor())
        self.assertIs(channel.overwrites[everyone].view_channel, False)
        report = await server_control.show_channels(guild, [channel], Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.restored, 1)
        self.assertIs(
            channel.overwrites[everyone].view_channel,
            True,
            "the allow that was there before the hide was not put back",
        )
        self.assertEqual(await db.hide_rows(guild.id), [], "the undo row is left behind")

    async def test_a_hidden_room_is_not_a_lock(self):
        """The sweep reads the same table, so the difference has to be a fact."""
        everyone = Role("@everyone")
        channel = Channel(1, "secrets")
        guild = self._server(channel)
        guild.default_role = everyone
        await server_control.hide_channels(guild, [channel], Actor())
        self.assertEqual(len(await db.hide_rows(guild.id)), 1, "no undo was written")
        self.assertEqual(await db.lockdown_locks(guild.id), [])
        self.assertEqual(await server_control.locked_channel_ids(guild.id), frozenset())
        self.assertFalse(await server_control.is_locked(guild.id, channel))

    async def test_unlocking_a_server_does_not_unhide_a_room(self):
        everyone = Role("@everyone")
        hidden, locked = Channel(1, "secrets"), Channel(2, "general")
        guild = self._server(hidden, locked)
        guild.default_role = everyone
        await server_control.hide_channels(guild, [hidden], Actor())
        await server_control.lock_channel(guild, locked, Actor())
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.restored, 1, "the lockdown was not the only thing lifted")
        self.assertIs(
            hidden.overwrites[everyone].view_channel,
            False,
            "a room somebody hid on purpose was put back on every list",
        )
        self.assertEqual(len(await db.hide_rows(guild.id)), 1)

    async def test_a_locked_room_is_refused_rather_than_converted(self):
        """One channel has one row, and a hide must not spend the lock's undo."""
        everyone = Role("@everyone")
        channel = Channel(1, "general")
        guild = self._server(channel)
        guild.default_role = everyone
        await server_control.lock_channel(guild, channel, Actor())
        row = await db.lockdown_rows(guild.id)
        report = await server_control.hide_channels(guild, [channel], Actor())
        self.assertFalse(report.ok)
        self.assertEqual(report.changed, 0)
        self.assertIn("lockdown", report.failures[0][1])
        self.assertEqual(await db.lockdown_rows(guild.id), row)

    async def test_a_category_takes_the_rooms_under_it(self):
        """Discord's rule decides it: a room with overwrites no longer follows
        its parent, so hiding the container alone leaves those rooms on the list.

        The stand-in is handed a ``channels`` list to make it a category, because
        that is the one attribute the question is asked about — a real
        ``discord.CategoryChannel`` cannot be built without a gateway state.
        """
        category = Channel(1, "archive", kind="category")
        inside = Channel(2, "notes")
        inside.category = category
        elsewhere = Channel(3, "general")
        category.channels = [inside]
        guild = self._server(category, inside, elsewhere)
        self.assertEqual(
            [room.id for room in server_control.hidden_scope(guild, category)], [1, 2]
        )
        self.assertEqual(
            [room.id for room in server_control.hidden_scope(guild, elsewhere)], [3]
        )

    async def test_a_whole_server_hide_covers_the_categories_and_the_rooms(self):
        """`everything` has no target to read, so the scope is the server itself.

        Containers belong in the loop beside their contents — a category is a room
        like any other to an overwrite, and hiding only the rooms would leave every
        heading standing over nothing — and a channel the bot cannot change stays
        **in** it: that is a refusal named on the card, where a channel quietly
        dropped out of the loop is a room the reader believes is hidden.
        """
        everyone = Role("@everyone")
        category = Channel(1, "archive", kind="category")
        inside = Channel(2, "notes")
        inside.category = category
        blocked = Channel(3, "theirs", manage=False)
        category.channels = [inside]
        guild = self._server(category, inside, blocked)
        guild.default_role = everyone
        self.assertEqual(
            [room.id for room in server_control.whole_server_scope(guild)], [1, 2, 3]
        )
        report = await server_control.hide_channels(
            guild, server_control.whole_server_scope(guild), Actor()
        )
        self.assertEqual(report.changed, 3, report.detail)
        for channel in (category, inside, blocked):
            self.assertIs(
                channel.overwrites[everyone].view_channel, False, channel.name
            )

    async def test_the_undo_of_a_whole_server_hide_is_the_ledger_not_the_list(self):
        """`show` over everything reads what was hidden, not what exists.

        A room the ledger never hid has nothing to put back, so walking the
        server's own channel list would answer *nothing of mine is hidden here*
        once per room and bury the ones that were hidden — and a room deleted while
        the hide held it costs nothing at all.
        """
        everyone = Role("@everyone")
        hidden = Channel(1, "secrets")
        deleted = Channel(2, "old-room")
        never = Channel(3, "general")
        guild = self._server(hidden, deleted, never)
        guild.default_role = everyone
        await server_control.hide_channels(guild, [hidden, deleted], Actor())
        guild.gone.add(2)
        held = await server_control.hidden_channels(guild)
        self.assertEqual([room.id for room in held], [1], "the ledger was not read")
        report = await server_control.show_channels(guild, held, Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.restored, 1)
        self.assertEqual(report.failures, [], "a room that was never hidden was walked")
        self.assertNotIn(everyone, hidden.overwrites, "the hide was not lifted")
        self.assertEqual(
            [row[0] for row in await db.hide_rows(guild.id)],
            [2],
            "the row left behind is the room that is gone, and only that one",
        )

    async def test_a_second_hide_is_not_a_refusal(self):
        everyone = Role("@everyone")
        channel = Channel(1, "secrets")
        guild = self._server(channel)
        guild.default_role = everyone
        await server_control.hide_channels(guild, [channel], Actor())
        report = await server_control.hide_channels(guild, [channel], Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.already, 1)
        self.assertEqual(report.failures, [])
        self.assertIn("already", report.sentence())

    async def test_it_names_the_ranks_discord_still_lets_in(self):
        """`Administrator` bypasses channel overwrites; the card says who has it."""
        plain = Role("Members", id=7)
        staff = Role("Staff", id=8, manage_channels=True)
        staff.permissions.administrator = True
        guild = self._server(roles=[plain, staff])
        named = [
            getattr(role, "name", "") for role in server_control.ranks_that_bypass(guild)
        ]
        self.assertEqual(named, ["Staff"])

    async def test_showing_a_room_that_was_never_hidden_says_so(self):
        everyone = Role("@everyone")
        channel = Channel(1, "secrets")
        guild = self._server(channel)
        guild.default_role = everyone
        report = await server_control.show_channels(guild, [channel], Actor())
        self.assertFalse(report.ok)
        self.assertIn("nothing of mine is hidden here", report.failures[0][1])
        self.assertEqual(channel.calls, [], "it changed a permission anyway")


class _FakeBot:
    """As much of a bot as the cog touches: the gate, the guilds, and readiness.

    ``guilds`` is empty by default, so the adoption pass the cog now starts in
    ``__init__`` runs once and finds nothing — a test that is not about adoption is
    not made to be about it.
    """

    def __init__(self, *, enabled=True, guilds=()):
        self.enabled = enabled
        self.guilds = list(guilds)
        self.user = SimpleNamespace(id=424242, bot=True)

    def is_module_enabled(self, _guild_id, _module):
        return self.enabled

    async def wait_until_ready(self):
        return True


def _forbidden() -> Exception:
    """Discord's own 403, built the way discord.py wants one."""
    import discord

    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no perms")


class _FakeMember:
    """A member as much as the sweep touches one: a DM and a timeout.

    ``send_raises`` is a closed inbox and ``timeout_raises`` is Discord refusing
    the timeout (a role above the bot's, or the owner) — the two refusals the
    sweep has to survive without losing the deletion.
    """

    def __init__(
        self,
        author_id=7,
        *,
        bot=False,
        name="member",
        send_raises=None,
        timeout_raises=None,
        administrator=False,
        roles=(),
    ):
        self.id = author_id
        self.bot = bot
        self.name = name
        self.display_name = name
        # Who a *hide* spares when this member is the one who asked for it.
        self.roles = list(roles)
        # The rank the sweep reads, as Discord reports it on a member: a whole
        # server's lockdown is the one that leaves it talking.
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.dms = []
        self.timeouts = []
        self._send_raises = send_raises
        self._timeout_raises = timeout_raises

    async def send(self, **payload):
        if self._send_raises is not None:
            raise self._send_raises
        self.dms.append(payload)

    async def timeout(self, until, *, reason=""):
        if self._timeout_raises is not None:
            raise self._timeout_raises
        self.timeouts.append((until, reason))


class _FakeMessage:
    """A message as much as the listener touches one."""

    def __init__(self, channel, *, author_bot=False, author_id=7, kind="default", author=None):
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.author = author if author is not None else _FakeMember(author_id, bot=author_bot)
        self.type = kind
        self.deleted = 0

    async def delete(self):
        self.deleted += 1


class _AuditEntry:
    """One audit-log entry, as much as the adoption pass reads of one.

    ``entry_id`` is Discord's snowflake, and it is what orders two entries that
    came out of different action reads — the pass keeps the highest one.
    """

    def __init__(self, channel_id, role_id, user_id, *, entry_id=1):
        self.id = entry_id
        self.target = SimpleNamespace(id=channel_id)
        self.extra = SimpleNamespace(id=role_id)
        self.user_id = user_id


class _AuditPage:
    """One page of entries, with a ``next()`` that says there is no more."""

    def __init__(self, entries):
        self._entries = list(entries)
        self.pages = 0

    def __aiter__(self):
        return self._walk()

    async def _walk(self):
        for entry in self._entries:
            yield entry

    async def next(self):
        self.pages += 1
        return False


class _AuditLog:
    """``guild.audit_logs(...)``. The action filter is Discord's; this fake answers
    every call with the same page, newest first, the way the real one is read —
    or a page per action type, for the laws about two actions disagreeing.

    ``by_action`` is keyed by the action's own name (``"overwrite_update"``).
    """

    def __init__(self, entries=(), *, by_action=None, raises=None):
        self.entries = list(entries)
        self.by_action = dict(by_action or {})
        self.raises = raises
        self.calls = []

    def __call__(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.calls.append(kwargs)
        action = kwargs.get("action")
        name = getattr(action, "name", str(action))
        return _AuditPage(self.by_action.get(name, self.entries))


class _Response:
    def __init__(self):
        self.messages = []
        self.deferred = 0

    def is_done(self):
        return False

    async def defer(self, **_kwargs):
        # A deferred reply is still a reply: `/hide` defers because its work is a
        # loop over every room in the server, and a fake without this would answer
        # the three-second window with an ``AttributeError`` instead of a card.
        self.deferred += 1

    async def send_message(self, content=None, **payload):
        # Discord's own signature is positional-first, and a fake that took only
        # keyword arguments answered a refusal with a ``TypeError`` — the shape of
        # fake that turns a law into a statement about the fake.
        self.messages.append({"content": content, **payload})


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **payload):
        self.messages.append({"content": content, **payload})


class _FakeInteraction:
    """Just enough of an interaction to drive the real command callbacks."""

    def __init__(self, guild, channel, *, user_id=42, name="R A J"):
        self.guild = guild
        self.guild_id = guild.id
        self.channel = channel
        self.user = SimpleNamespace(
            id=user_id, display_name=name, name=name.lower(), mention=f"<@{user_id}>"
        )
        self.response = _Response()
        self.followup = _Followup()

    async def edit_original_response(self, **_payload):
        return None  # nothing here edits its own reply, but send_card looks for it


def _payload_text(payload) -> str:
    """Everything a card payload says, whether it went out as a layout or an embed."""
    view = payload.get("view")
    if view is not None and hasattr(view, "to_components"):
        parts: list = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("content"), str):
                    parts.append(node["content"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(view.to_components())
        return " ".join(parts)
    embed = payload.get("embed")
    if embed is not None:
        return f"{getattr(embed, 'title', '')} {getattr(embed, 'description', '')}"
    return str(payload.get("content") or "")


class ThePlan(unittest.TestCase):
    def test_it_locks_text_and_leaves_voice_alone(self):
        text = Channel(1, "general")
        voice = Channel(2, "lounge", kind="voice")
        guild = Guild(channels=[text, voice])
        plan = server_control.plan(guild)
        self.assertEqual([channel.id for channel in plan.change], [1])
        self.assertEqual([channel.id for channel in plan.skipped], [2])

    def test_a_channel_already_locked_is_reported_rather_than_locked_again(self):
        everyone = Role("@everyone")
        locked = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})
        guild = Guild(channels=[locked])
        guild.default_role = everyone
        plan = server_control.plan(guild)
        self.assertEqual(plan.change, [])
        self.assertEqual(len(plan.already_locked), 1)
        self.assertIn("already locked", plan.sentence())

    def test_a_channel_it_cannot_manage_is_named_with_the_permission_it_needs(self):
        guild = Guild(channels=[Channel(1, "other", manage=False)])
        plan = server_control.plan(guild)
        self.assertEqual(len(plan.blocked), 1)
        self.assertEqual(plan.blocked[0][1], "Manage Channels")
        self.assertIn("cannot manage", plan.sentence())

    def test_an_administrator_cannot_lock_nothing_and_be_told_it_worked(self):
        guild = Guild(channels=[])
        plan = server_control.plan(guild)
        self.assertFalse(plan.worth_changing)
        self.assertEqual(plan.counts["change"], 0)


class TheLockdown(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        self.order = []

    def tearDown(self):
        self.release_temp_database()

    def _guild(self, *channels):
        guild = Guild(channels=list(channels))
        # The ledger must be written before the first permission changes.
        real = server_control.db.save_lockdown

        async def watched(guild_id, rows, by, at, *, scope="channel"):
            self.order.append("ledger")
            return await real(guild_id, rows, by, at, scope=scope)

        return guild, watched

    async def test_the_ledger_is_written_before_anything_is_changed(self):
        guild, watched = self._guild(Channel(1, "general"), Channel(2, "memes"))
        with mock.patch.object(server_control.db, "save_lockdown", new=watched):
            report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 2)
        self.assertEqual(self.order, ["ledger"], "the undo was recorded first")
        self.assertEqual(len(await db.lockdown_rows(guild.id)), 2)

    async def test_the_deny_removes_talking_threads_and_reactions(self):
        channel = Channel(1, "general")
        guild, watched = self._guild(channel)
        with mock.patch.object(server_control.db, "save_lockdown", new=watched):
            await server_control.lockdown(guild, Actor())
        _kind, _target, overwrite, reason = channel.calls[0]
        self.assertIs(overwrite.send_messages, False)
        self.assertIs(overwrite.create_public_threads, False)
        self.assertIs(overwrite.add_reactions, False)
        self.assertIn("lockdown", reason)

    async def test_a_channel_discord_refuses_does_not_stop_the_others(self):
        good, bad = Channel(1, "general"), Channel(2, "locked-out")
        guild, watched = self._guild(good, bad)

        async def refuse(target, *, overwrite=None, reason=""):
            raise RuntimeError("nope")

        with mock.patch.object(server_control.db, "save_lockdown", new=watched), \
                mock.patch.object(bad, "set_permissions", new=refuse):
            report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok, "one refusal is not a failed lockdown")
        self.assertEqual(report.changed, 1)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("locked-out", report.failures[0][0])

    async def test_a_ledger_that_cannot_be_written_means_nothing_happens(self):
        channel = Channel(1, "general")
        guild = Guild(channels=[channel])
        with mock.patch.object(server_control.db, "save_lockdown", side_effect=RuntimeError("disk full")):
            report = await server_control.lockdown(guild, Actor())
        self.assertFalse(report.ok)
        self.assertEqual(channel.calls, [], "a change with no undo is not worth making")
        self.assertIn("undo", report.detail)

    async def test_an_already_locked_server_is_a_success_with_nothing_to_do(self):
        everyone = Role("@everyone")
        guild = Guild(channels=[Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})])
        guild.default_role = everyone
        report = await server_control.lockdown(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 0)
        self.assertEqual(await db.lockdown_rows(guild.id), [])


class TheUndo(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()

    def tearDown(self):
        self.release_temp_database()

    async def test_it_puts_back_the_overwrite_that_was_there(self):
        """The whole reason the ledger exists: an undo that blanket-allows is a
        change nobody asked for."""
        everyone = Role("@everyone")
        channel = Channel(1, "quiet", overwrites={everyone: Overwrite(allow=0, deny=1 << 11)})
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lockdown(guild, Actor())
        channel.calls.clear()
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.restored, 1)
        _kind, _target, overwrite, _reason = channel.calls[0]
        self.assertIsNotNone(overwrite, "the overwrite that was there is restored")
        _allow, deny = overwrite.pair()
        self.assertEqual(deny.value, 1 << 11, "the deny the server already had")
        self.assertEqual(await db.lockdown_rows(guild.id), [], "the ledger is tidied")

    async def test_an_overwrite_that_did_not_exist_is_removed_not_allowed(self):
        everyone = Role("@everyone")
        channel = Channel(1, "general")
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lockdown(guild, Actor())
        self.assertIn(everyone, channel.overwrites)
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertNotIn(everyone, channel.overwrites, "there was nothing here before the lockdown")
        self.assertEqual(channel.calls[-1][2], None)

    async def test_a_channel_deleted_during_a_lockdown_does_not_hold_the_undo_open(self):
        guild = Guild(channels=[Channel(1, "general")])
        await server_control.lockdown(guild, Actor())
        guild.gone.add(1)
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(await db.lockdown_rows(guild.id), [])

    async def test_a_server_that_was_never_locked_says_so_rather_than_reverting_things(self):
        guild = Guild(channels=[Channel(1, "general")])
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertIn("No lockdown", report.detail)
        self.assertEqual(guild.channels[0].calls, [])

    async def test_only_the_channels_it_restored_leave_the_ledger(self):
        good, bad = Channel(1, "general"), Channel(2, "locked-out")
        guild = Guild(channels=[good, bad])
        await server_control.lockdown(guild, Actor())

        async def refuse(target, *, overwrite=None, reason=""):
            raise RuntimeError("nope")

        with mock.patch.object(bad, "set_permissions", new=refuse):
            report = await server_control.unlock(guild, Actor())
        self.assertEqual(report.restored, 1)
        self.assertEqual(report.blocked, 1)
        left = [channel_id for channel_id, _p, _a in await db.lockdown_rows(guild.id)]
        self.assertEqual(left, [2], "the one that failed is still owed an undo")


class ThePerChannelLock(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """One channel, the same ledger — and the same rows the listener reads.

    The per-channel `/lockdown` used to write a permission and nothing else, so
    the only way to open that channel was to allow `@everyone` to talk. It keeps
    the ledger now, which is also what makes a locked channel's messages removable
    without a second registry to disagree with the undo.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    async def test_a_lock_forgets_the_ladder_the_last_incident_built(self):
        """The ladder is per incident: a fresh lock is not the one somebody was climbing."""
        guild = Guild(channels=[Channel(1, "general")])
        for _ in range(server_control.SPAM_LIMIT - 1):
            server_control.note_locked_message(guild.id, 1, 3, now=0.0)
        await server_control.lock_channel(guild, guild.channels[0], Actor())
        self.assertIsNone(server_control.note_locked_message(guild.id, 1, 3, now=0.1))

    async def test_an_unlock_forgets_the_ladder_too(self):
        guild = Guild(channels=[Channel(1, "general")])
        await server_control.lock_channel(guild, guild.channels[0], Actor())
        for _ in range(server_control.SPAM_LIMIT):
            server_control.note_locked_message(guild.id, 1, 3, now=0.0)
        await server_control.unlock_channel(guild, guild.channels[0], Actor())
        self.assertIsNone(server_control.note_locked_message(guild.id, 1, 3, now=0.1))

    async def test_one_channel_is_recorded_by_the_same_ledger(self):
        channel = Channel(1, "general")
        guild = Guild(channels=[channel])
        report = await server_control.lock_channel(guild, channel, Actor())
        self.assertTrue(report.ok)
        rows = await db.lockdown_rows(guild.id)
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(await server_control.locked_channel_ids(guild.id), frozenset({1}))
        self.assertIs(channel.overwrites[guild.default_role].send_messages, False)

    async def test_a_second_lock_keeps_the_overwrite_from_before_the_first(self):
        """The row is the undo. Re-recording it with the lockdown's own deny would
        leave the undo restoring the *lock*."""
        everyone = Role("@everyone")
        channel = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=1 << 11)})
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lockdown(guild, Actor())
        original = await db.lockdown_rows(guild.id)
        await server_control.lock_channel(guild, channel, Actor())
        self.assertEqual(await db.lockdown_rows(guild.id), original)

    async def test_a_second_lock_on_a_channel_that_already_holds_is_not_a_refusal(self):
        """Nothing to change is not Discord refusing the change.

        The row is written either way — that is what makes the channel mine from
        here on — so the red *Discord refused the permission change — I need Manage
        Channels there* card was the command contradicting its own ledger. A
        `/lockdown` on a channel that already carries the deny (which is how an
        administrator closes one of the orphans `/check` reports) answered with a
        permission problem nobody had.
        """
        channel = Channel(1, "quiet")
        guild = Guild(channels=[channel])
        await server_control.lock_channel(guild, channel, Actor())
        written = len(channel.calls)

        report = await server_control.lock_channel(guild, channel, Actor())

        self.assertTrue(report.ok, report.sentence())
        self.assertEqual(report.changed, 0)
        self.assertEqual(report.already, 1)
        self.assertEqual(len(channel.calls), written, "a second lock wrote a permission")
        self.assertIn("Nothing needed changing", report.sentence())
        self.assertNotIn("refused", report.sentence())
        self.assertEqual(len(await db.lockdown_rows(guild.id)), 1)

    async def test_unlocking_one_channel_leaves_the_others_locked(self):
        first, second = Channel(1, "general"), Channel(2, "memes")
        guild = Guild(channels=[first, second])
        await server_control.lockdown(guild, Actor())
        report = await server_control.unlock_channel(guild, first, Actor())
        self.assertTrue(report.ok)
        left = await db.lockdown_rows(guild.id)
        self.assertEqual([row[0] for row in left], [2], "only the one that was released")
        self.assertEqual(await server_control.locked_channel_ids(guild.id), frozenset({2}))
        self.assertNotIn(guild.default_role, first.overwrites)

    async def test_a_channel_locked_before_there_was_a_ledger_is_still_unlocked(self):
        """No row, an overwrite: the honest undo is removing it, not allowing it."""
        everyone = Role("@everyone")
        channel = Channel(1, "general", overwrites={everyone: Overwrite(deny=1 << 11)})
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        report = await server_control.unlock_channel(guild, channel, Actor())
        self.assertTrue(report.ok)
        self.assertNotIn(everyone, channel.overwrites, "the deny was removed, not inverted")
        self.assertFalse(await server_control.is_locked(guild.id, channel))

    async def test_a_failed_reading_answers_nothing_is_locked(self):
        """A database that could not be read is not a reason to delete messages."""
        guild = Guild(channels=[Channel(1, "general")])
        server_control.forget_lock_cache()
        with mock.patch.object(
            server_control.db, "lockdown_locks", side_effect=RuntimeError("disk gone")
        ):
            self.assertFalse(await server_control.is_locked(guild.id, guild.channels[0]))

    async def test_a_thread_is_held_by_the_channel_it_hangs_under(self):
        """The lockdown denies *creating* a public thread; one that existed is
        still a place to type, and its permissions are its parent's."""
        guild = Guild(channels=[Channel(1, "general")])
        await server_control.lock_channel(guild, guild.channels[0], Actor())
        thread = SimpleNamespace(id=99, parent_id=1)
        self.assertTrue(await server_control.is_locked(guild.id, thread))


class TheLocksOlderThanTheLedger(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """A channel locked before the ledger existed arms itself at boot.

    Every ``/lockdown`` before the last pass wrote a permission and nothing else,
    which left channels that are locked *and unrecorded* — a deny the sweep walks
    straight past, because the row is what it reads. The old lock in the beta
    server is one of those, and the workaround was an ``/unlock`` and a second
    ``/lockdown`` to make the bot notice.

    Adoption writes the missing row, and only for the locks Discord's audit log
    proves are this bot's own: a channel an administrator denied by hand is staff
    talking among themselves, and deleting their messages would be this feature
    mistaking somebody else's decision for its own.
    """

    BOT_ID = 424242
    #: The id every stand-in ``@everyone`` in this file carries (``Role`` above).
    EVERYONE_ID = 1

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _everyone(self):
        # `@everyone` can talk server-wide here, which is the floor every channel's
        # permissions start from: an overwrite that grants Send Messages where the
        # role already grants it takes nothing away when it is removed.
        role = Role("@everyone")
        role.permissions.send_messages = True
        return role

    def _old_lock(self, everyone, channel_id=1, *, name="quiet", allow=0, kind="text", manage=True):
        """A channel holding exactly the overwrite a pre-ledger `/lockdown` left."""
        return Channel(
            channel_id,
            name,
            kind=kind,
            manage=manage,
            overwrites={everyone: Overwrite(allow=allow, deny=Bits.NAMES["send_messages"])},
        )

    def _server(self, everyone, *channels, entries=()):
        guild = Guild(channels=list(channels), audit_logs=_AuditLog(entries))
        guild.default_role = everyone
        for channel in channels:
            channel.guild = guild
        return guild

    def _entry(self, channel_id=1, *, user_id=None, role_id=None):
        """One `@everyone` overwrite change, by this bot unless told otherwise."""
        return _AuditEntry(
            channel_id,
            self.EVERYONE_ID if role_id is None else role_id,
            self.BOT_ID if user_id is None else user_id,
        )

    def _bot(self):
        return SimpleNamespace(id=self.BOT_ID)

    async def test_the_shape_of_the_old_command_is_denied_send_and_nothing_allowed(self):
        send = Bits.NAMES["send_messages"]
        self.assertTrue(server_control.looks_like_our_lock(Overwrite(deny=send)))
        self.assertTrue(
            server_control.looks_like_our_lock(
                Overwrite(deny=send | Bits.NAMES["create_public_threads"])
            ),
            "the server-wide lockdown denies more permissions, and is the same lock",
        )
        self.assertFalse(
            server_control.looks_like_our_lock(Overwrite(allow=1 << 10, deny=send)),
            "an allow bit means somebody has changed this since the old command ran",
        )
        self.assertFalse(server_control.looks_like_our_lock(Overwrite(allow=send)))
        self.assertFalse(server_control.looks_like_our_lock(Overwrite()))
        self.assertFalse(server_control.looks_like_our_lock(None))

    async def test_a_mask_a_person_made_is_not_our_lock(self):
        """The live server this was written from, on the screen its owner sent.

        A *private* category carries a wall — the one in the screenshot denies
        **39** permissions, View Channels among them — and it denies Send Messages
        and allows nothing, which is the two facts the old signature asked for. The
        card therefore told its owner that the bot's own old lockdown was sitting on
        a category he had configured by hand, and that `fix` would leave it alone:
        a wrong claim and a dead end in one sentence. The signature now also asks
        *what else* is denied, because a lockdown only ever writes the bits a
        lockdown writes.
        """
        wall = (
            Bits.NAMES["view_channel"]
            | Bits.NAMES["send_messages"]
            | (1 << 4)   # Manage Channels
            | (1 << 29)  # Manage Webhooks
            | (1 << 0)   # Create Invite
        )

        self.assertFalse(server_control.looks_like_our_lock(Overwrite(deny=wall)))
        self.assertFalse(
            server_control.looks_like_our_lock(
                Overwrite(deny=Bits.NAMES["send_messages"] | (1 << 4))
            ),
            "one permission a lockdown never denies is enough to tell the two apart",
        )

    async def test_a_hide_that_is_not_the_whole_lock_is_not_ours(self):
        """`View Channel` has only ever been denied *beside* the three.

        The hidden lockdown arrived in one commit with `LOCKED_PERMISSIONS` already
        there, so a mask of Send Messages + View Channel and nothing else is
        somebody's own room: the Admin category on the live server, where the owner
        hid a category by hand. Staff hiding a room among themselves is not a lock
        of ours to claim, and the pass that adopts old locks must not adopt it.
        """
        send, threads, reactions, view = (
            Bits.NAMES["send_messages"],
            Bits.NAMES["create_public_threads"],
            Bits.NAMES["add_reactions"],
            Bits.NAMES["view_channel"],
        )

        self.assertFalse(server_control.looks_like_our_lock(Overwrite(deny=send | view)))
        self.assertTrue(
            server_control.looks_like_our_lock(Overwrite(deny=send | threads | reactions | view)),
            "the hidden lockdown writes the whole mask, and is ours",
        )

    async def test_the_shape_is_read_from_discords_object_and_not_an_attribute_it_lacks(self):
        """`discord.PermissionOverwrite` has ``pair()`` and **no** ``allow``/
        ``deny`` attributes.

        Reading one off it by name answered ``None``, which made every overwrite
        look like *denies nothing, allows nothing* — so `plan()` never called a
        channel already locked and the adoption pass could never have claimed a
        lock on a real server, while both passed here against a stand-in that had
        the attribute. Every other law in this file uses the stand-in (shaped like
        Discord's object now); this one is the real object, so the two agree about
        what is being read rather than about what a fake happens to offer.
        """
        import discord

        send = Bits.NAMES["send_messages"]
        self.assertTrue(
            server_control.looks_like_our_lock(discord.PermissionOverwrite(send_messages=False))
        )
        self.assertFalse(
            server_control.looks_like_our_lock(
                discord.PermissionOverwrite(view_channel=True, send_messages=False)
            )
        )
        self.assertEqual(
            server_control._describe(discord.PermissionOverwrite(send_messages=False)),
            f"0:{send}",
            "the ledger does not store what Discord's own object holds",
        )
        self.assertTrue(
            server_control._allows(
                discord.PermissionOverwrite(send_messages=True), ("send_messages",)
            ),
            "an allow on Discord's own object did not read as one",
        )

    async def test_the_mark_the_old_unlock_left_is_recognised_and_only_it(self):
        """The fingerprint of the bug, exact: *allow Send Messages, deny nothing*.

        `/unlock` before the ledger opened a room by ``set_permissions(everyone,
        send_messages=True)`` — an overwrite in its own right, which is the shape
        that makes Discord stop applying the room's category to it. The mark is
        read off Discord's own enum rather than typed beside it, and every other
        shape is somebody's decision rather than a leftover.
        """
        import discord

        send = Bits.NAMES["send_messages"]
        self.assertTrue(server_control.is_stray_unlock(Overwrite(allow=send)))
        self.assertFalse(
            server_control.is_stray_unlock(Overwrite(allow=send, deny=send)),
            "a mark that also denies something is not what that command wrote",
        )
        self.assertFalse(
            server_control.is_stray_unlock(Overwrite(allow=send | Bits.NAMES["add_reactions"])),
            "a permission the old command never wrote is a person's choice",
        )
        self.assertFalse(
            server_control.is_stray_unlock(Overwrite(deny=send)), "a lock is not a stray"
        )
        self.assertFalse(server_control.is_stray_unlock(Overwrite()))
        self.assertFalse(server_control.is_stray_unlock(None))
        self.assertTrue(
            server_control.is_stray_unlock(discord.PermissionOverwrite(send_messages=True)),
            "Discord's own object is the shape this has to recognise",
        )

    async def test_a_room_the_old_unlock_opened_is_returned_to_its_category(self):
        """The undo: the overwrite is **removed**, which is the state before the bug.

        Nothing else about the room is written — no deny, no allow, no ledger row —
        because the room had no overwrite at all before that command ran, so
        removing one *is* the restore.
        """
        everyone = self._everyone()
        channel = Channel(
            1,
            "video-idea",
            overwrites={everyone: Overwrite(allow=Bits.NAMES["send_messages"])},
        )
        guild = self._server(everyone, channel)

        done, why = await server_control.undo_stray_unlock(guild, channel, Actor())

        self.assertTrue(done, why)
        self.assertEqual(why, "")
        self.assertNotIn(everyone, channel.overwrites, "the old unlock's overwrite is still there")
        self.assertEqual([call[0] for call in channel.calls], ["set_permissions"])
        _name, target, overwrite, reason = channel.calls[0]
        self.assertIs(target, everyone)
        self.assertIsNone(overwrite)
        self.assertIn("stray unlock", reason)

    async def test_no_other_overwrite_is_ever_taken_away(self):
        """Every other shape is somebody's decision, and this is not its undo."""
        everyone = self._everyone()
        send = Bits.NAMES["send_messages"]
        for name, mark in (
            ("a lock's deny", Overwrite(deny=send)),
            ("a room allowed to react as well", Overwrite(allow=send | Bits.NAMES["add_reactions"])),
            ("an overwrite that grants and denies nothing", Overwrite()),
            ("an administrator's hide", Overwrite(deny=Bits.NAMES["view_channel"] | (1 << 4))),
        ):
            with self.subTest(name):
                channel = Channel(1, "general", overwrites={everyone: mark})
                guild = self._server(everyone, channel)

                done, why = await server_control.undo_stray_unlock(guild, channel, Actor())

                self.assertFalse(done, f"{name} was removed as a stray unlock")
                self.assertTrue(why, "a refusal that says nothing is not a refusal")
                self.assertIn(everyone, channel.overwrites)
                self.assertEqual(channel.calls, [], f"{name}: a permission was changed")

    async def test_a_server_where_only_that_overwrite_allows_speech_keeps_it(self):
        """The guard: an undo must not silence a room on the way past.

        Where `@everyone`'s own role denies Send Messages and channels opt in one by
        one, the overlay an old `/unlock` wrote is that room's **only** source of the
        permission. Removing it there would take it away rather than put anything
        back, so it is named and left to a person.
        """
        everyone = self._everyone()
        channel = Channel(
            1,
            "video-idea",
            overwrites={everyone: Overwrite(allow=Bits.NAMES["send_messages"])},
        )
        guild = self._server(everyone, channel)
        everyone.permissions.send_messages = False

        self.assertFalse(server_control.everyone_can_send(guild))
        self.assertFalse(server_control.stray_unlock_is_undoable(guild, channel))
        done, why = await server_control.undo_stray_unlock(guild, channel, Actor())

        self.assertFalse(done, "the room was silenced by its own undo")
        self.assertIn("only", why)
        self.assertIn(everyone, channel.overwrites)
        self.assertEqual(channel.calls, [])

    async def test_a_room_with_no_overwrite_of_its_own_is_already_at_rest(self):
        """The synced case: the category applies, and there is nothing to undo."""
        everyone = self._everyone()
        channel = Channel(1, "video-idea")
        guild = self._server(everyone, channel)

        done, why = await server_control.undo_stray_unlock(guild, channel, Actor())

        self.assertTrue(done, why)
        self.assertEqual(channel.calls, [], "a permission was changed on a synced room")

    async def test_an_administrators_own_deny_is_left_exactly_where_it_is(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel, entries=[self._entry(user_id=99)])
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual(adopted, [], "somebody else's lock was adopted as ours")
        self.assertEqual(await db.lockdown_rows(guild.id), [])
        self.assertEqual(channel.calls, [], "adoption must not change a permission")

    async def test_the_bots_own_old_lock_is_written_down_and_nothing_else_changes(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel, entries=[self._entry()])
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual([getattr(one, "id", 0) for one in adopted], [1])
        rows = await db.lockdown_rows(guild.id)
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(rows[0][1], "", "the old lock left no overwrite to put back")
        self.assertEqual(channel.calls, [], "the deny is already there; the row is the work")

    async def test_an_adopted_lock_is_held_until_one_command_opens_it(self):
        """The whole point: the old channel is enforced without a relock, and
        `/unlock` removes the deny rather than restoring a lock nobody wrote down."""
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel, entries=[self._entry()])
        await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertTrue(await server_control.is_locked(guild.id, channel))
        channel.calls.clear()
        report = await server_control.unlock(guild, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.restored, 1)
        _kind, _target, overwrite, _reason = channel.calls[-1]
        self.assertIsNone(overwrite, "the old lock's deny is removed, not inverted")
        self.assertEqual(await db.lockdown_rows(guild.id), [])

    async def test_the_cog_arms_the_old_lock_at_boot_and_says_so_in_the_log(self):
        from cogs.server_control import ServerControl

        everyone = self._everyone()
        channel = self._old_lock(everyone, name="premier")
        guild = self._server(everyone, channel, entries=[self._entry()])
        cog = ServerControl(bot=_FakeBot(enabled=True, guilds=[guild]))
        cog.adopt.cancel()  # the boot task; this test drives that same body once, itself
        with mock.patch.object(modlog, "post", new=mock.AsyncMock(return_value=True)) as posted:
            await cog.adopt.coro(cog)
        posted.assert_awaited_once()
        self.assertIn("premier", posted.await_args.kwargs["embed"].description)
        self.assertEqual([row[0] for row in await db.lockdown_rows(guild.id)], [1])

        # And the lock it just armed is enforced by the same listener as `/lockdown`.
        member = _FakeMember(author_id=7)
        message = _FakeMessage(channel, author=member)
        await cog.on_message(message)
        self.assertEqual(message.deleted, 1, "the adopted lock does not hold")
        self.assertEqual(len(member.dms), 1, "the sender was not warned")

    async def test_a_channel_the_ledger_already_knows_is_not_adopted_again(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel, entries=[self._entry()])
        await db.save_lockdown(guild.id, [(1, "0:2048")], 0, 0.0)
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual(adopted, [])
        rows = await db.lockdown_rows(guild.id)
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(rows[0][1], "0:2048", "the undo from before the lock was overwritten")

    async def test_a_lock_that_allows_something_is_not_only_ours(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone, allow=1 << 10)
        guild = self._server(everyone, channel, entries=[self._entry()])
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual(adopted, [], "an allow bit means somebody edited this after the old lock")

    async def test_a_voice_channel_is_not_a_lockdown(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone, kind="voice")
        guild = self._server(everyone, channel, entries=[self._entry()])
        self.assertEqual(await server_control.adopt_legacy_locks(guild, self._bot()), [])

    async def test_a_channel_it_cannot_manage_is_left_out(self):
        """A row for a channel whose lock it cannot lift would be a trap: the
        messages are removed and `/unlock` cannot open the room again."""
        everyone = self._everyone()
        channel = self._old_lock(everyone, manage=False)
        guild = self._server(everyone, channel, entries=[self._entry()])
        self.assertEqual(await server_control.adopt_legacy_locks(guild, self._bot()), [])

    async def test_a_channel_the_audit_log_never_mentions_is_not_adopted(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel)
        self.assertEqual(await server_control.adopt_legacy_locks(guild, self._bot()), [])

    async def test_an_entry_about_another_role_does_not_speak_for_everyone(self):
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel, entries=[self._entry(role_id=99)])
        self.assertEqual(await server_control.adopt_legacy_locks(guild, self._bot()), [])

    async def test_each_channel_is_decided_by_its_own_entries(self):
        """One page holds both channels, and only the one the bot changed is ours."""
        everyone = self._everyone()
        mine, theirs = self._old_lock(everyone, 1), self._old_lock(everyone, 2)
        guild = self._server(
            everyone, mine, theirs,
            entries=[self._entry(1), self._entry(2, user_id=99)],
        )
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual([getattr(one, "id", 0) for one in adopted], [1])

    async def test_the_newest_change_of_any_kind_is_the_one_that_decides(self):
        """The three overwrite actions are separate reads, so the decision is by
        entry id rather than by which page came back first: a bot edit from before
        an administrator's newer hand-deny is not evidence the deny is ours, and
        the reverse is a lock this bot placed after their older one."""
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        theirs_newer = self._server(everyone, channel)
        theirs_newer._audit = _AuditLog(by_action={
            "overwrite_update": [_AuditEntry(1, everyone.id, self.BOT_ID, entry_id=100)],
            "overwrite_create": [_AuditEntry(1, everyone.id, 99, entry_id=200)],
        })
        self.assertEqual(
            await server_control.adopt_legacy_locks(theirs_newer, self._bot()),
            [],
            "an older edit of ours was read as today's deny",
        )

        mine_newer = self._server(everyone, self._old_lock(everyone))
        mine_newer._audit = _AuditLog(by_action={
            "overwrite_update": [_AuditEntry(1, everyone.id, self.BOT_ID, entry_id=300)],
            "overwrite_create": [_AuditEntry(1, everyone.id, 99, entry_id=200)],
        })
        adopted = await server_control.adopt_legacy_locks(mine_newer, self._bot())
        self.assertEqual([getattr(one, "id", 0) for one in adopted], [1])

    async def test_a_guild_where_every_channel_holds_the_old_lock_is_one_server_lockdown(self):
        """The old server-wide command left the same overwrite on every channel and
        nothing else does, so the adoption leaves staff able to talk through it."""
        everyone = self._everyone()
        guild = self._server(
            everyone,
            self._old_lock(everyone, 1),
            self._old_lock(everyone, 2),
            entries=[self._entry(1), self._entry(2)],
        )
        with contextlib.redirect_stdout(io.StringIO()) as said:
            adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual(len(adopted), 2)
        self.assertEqual(
            {scope for _channel_id, scope in await db.lockdown_locks(guild.id)}, {"server"}
        )
        self.assertIn("as one server-wide lockdown", said.getvalue())

    async def test_a_lock_on_one_of_several_channels_is_adopted_as_that_channel_only(self):
        everyone = self._everyone()
        guild = self._server(
            everyone, self._old_lock(everyone, 1), Channel(2, "open"), entries=[self._entry(1)]
        )
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual([getattr(one, "id", 0) for one in adopted], [1])
        self.assertEqual(dict(await db.lockdown_locks(guild.id))[1], "channel")

    async def test_the_real_async_generator_shape_is_consumed_and_adopts(self):
        """discord.py 2.x's ``audit_logs`` is an **async generator**: it has no
        ``.next()`` to await, which is what the paging loop called — so every real
        read raised ``AttributeError`` and **no old lock was ever adopted**, while
        every law here passed against a 1.x-shaped fake. The fake below is the
        real shape, and it is what found the defect in the first place: the boot
        log said *0 candidate(s) adopted* on a guild holding ten of them."""
        everyone = self._everyone()
        channel = self._old_lock(everyone)
        guild = self._server(everyone, channel)

        async def real_shape(**_kwargs):
            yield _AuditEntry(1, everyone.id, self.BOT_ID)

        guild._audit = real_shape
        adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual([getattr(one, "id", 0) for one in adopted], [1])

    async def test_an_audit_log_that_cannot_be_read_adopts_nothing(self):
        everyone = self._everyone()
        guild = self._server(
            everyone,
            self._old_lock(everyone),
            entries=[self._entry()],
        )
        guild._audit = _AuditLog(raises=RuntimeError("audit log unavailable"))
        with contextlib.redirect_stdout(io.StringIO()) as said:
            adopted = await server_control.adopt_legacy_locks(guild, self._bot())
        self.assertEqual(adopted, [], "a log that could not be read is not evidence")
        self.assertEqual(await db.lockdown_rows(guild.id), [])
        self.assertIn("could not read the audit log", said.getvalue())

    async def test_every_server_says_what_the_pass_looked_at(self):
        """A pass that says nothing when it finds nothing cannot be told from a
        pass that never ran — which is how a deploy is read back from a log ring."""
        everyone = self._everyone()
        theirs = self._server(everyone, self._old_lock(everyone), entries=[self._entry(user_id=99)])
        with contextlib.redirect_stdout(io.StringIO()) as said:
            await server_control.adopt_legacy_locks(theirs, self._bot())
        self.assertIn(f"guild {theirs.id}", said.getvalue())
        self.assertIn("1 candidate(s), adopted 0", said.getvalue())

        mine_everyone = self._everyone()
        mine = self._server(
            mine_everyone, self._old_lock(mine_everyone), entries=[self._entry()]
        )
        with contextlib.redirect_stdout(io.StringIO()) as said:
            adopted = await server_control.adopt_legacy_locks(mine, self._bot())
        self.assertEqual(len(adopted), 1)
        self.assertIn("1 candidate(s), adopted 1 (#quiet)", said.getvalue())


class TheListenerRemovesWhatWasPostedWhileLocked(
    TempDatabaseMixin, unittest.IsolatedAsyncioTestCase
):
    """
    Discord's permissions cannot stop an administrator, and a channel that still
    had an administrator talking in it after `/lockdown` is the defect this covers:
    the deny reads as locked, and the sweep is what makes it one.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()
        from cogs.server_control import ServerControl

        self.cog = ServerControl(bot=_FakeBot(enabled=True))
        self.channel = Channel(1, "general")
        self.guild = Guild(channels=[self.channel])
        await server_control.lock_channel(self.guild, self.channel, Actor())

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    async def _burst(self, member, count=None):
        """Send ``count`` messages into the locked channel, as the sweep sees them."""
        count = server_control.SPAM_LIMIT if count is None else count
        for _ in range(count):
            await self.cog.on_message(_FakeMessage(self.channel, author=member))

    async def test_every_removed_message_warns_its_sender_by_dm(self):
        member = _FakeMember(author_id=7)
        await self.cog.on_message(_FakeMessage(self.channel, author=member))
        self.assertEqual(len(member.dms), 1, "the sender was not told")
        said = _payload_text(member.dms[0])
        self.assertIn("locked", said)
        self.assertIn("removed", said)
        self.assertIn("administrators and the server's owner included", said)
        self.assertIn("timed out", said, "the strict part is not on the card")

    async def test_a_burst_is_timed_out_whether_or_not_they_are_an_administrator(self):
        member = _FakeMember(author_id=7)
        await self._burst(member, server_control.SPAM_LIMIT - 1)
        self.assertEqual(member.timeouts, [], "four messages are not a burst")
        await self._burst(member, 1)
        self.assertEqual(len(member.timeouts), 1, "the fifth message was not a burst")
        until, reason = member.timeouts[0]
        self.assertEqual(until, datetime.timedelta(seconds=server_control.SPAM_TIMEOUTS[0]))
        self.assertIn("locked", reason)
        self.assertIn("10 minutes", _payload_text(member.dms[-1]))

    async def test_the_timeout_is_recorded_as_a_case(self):
        member = _FakeMember(author_id=7)
        with mock.patch.object(moderation_actions, "record", new=mock.AsyncMock()) as recorded:
            await self._burst(member)
        recorded.assert_awaited_once()
        guild_arg, action, target_arg = recorded.await_args.args[:3]
        self.assertEqual(action, "timeout")
        self.assertIs(target_arg, member)
        self.assertEqual(guild_arg, self.guild)

    async def test_a_refused_timeout_is_told_to_the_member_and_written_to_the_log(self):
        member = _FakeMember(author_id=7, timeout_raises=_forbidden())
        with mock.patch.object(modlog, "post", new=mock.AsyncMock(return_value=True)) as posted:
            await self._burst(member)
        self.assertEqual(member.timeouts, [])
        posted.assert_awaited()
        self.assertIn("refused", _payload_text(member.dms[-1]))

    async def test_the_owner_is_warned_and_discord_is_never_asked_to_time_them_out(self):
        """No bot may time out the server's owner: the attempt would be a 403 every
        time, and saying so on the card is better than pretending it landed."""
        self.guild.owner_id = 42
        member = _FakeMember(author_id=42)
        await self._burst(member)
        self.assertEqual(member.timeouts, [])
        self.assertIn("owner", _payload_text(member.dms[-1]))

    async def test_a_closed_inbox_does_not_stop_the_removal(self):
        member = _FakeMember(author_id=7, send_raises=_forbidden())
        message = _FakeMessage(self.channel, author=member)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 1)

    async def test_the_warning_goes_to_the_member_and_not_into_the_locked_room(self):
        """The room is the thing being kept silent, and a bot line under a deleted
        message is a conversation — so the warning is a DM, and this channel fake
        has no ``send`` for one to slip into."""
        member = _FakeMember(author_id=7)
        await self.cog.on_message(_FakeMessage(self.channel, author=member))
        self.assertEqual(len(member.dms), 1, "the warning was not delivered to the member")

    async def test_it_removes_an_administrators_message_like_any_other(self):
        message = _FakeMessage(self.channel, author_id=7)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 1, "the administrator's message was left behind")

    async def test_it_leaves_the_bots_own_card_where_the_channel_can_read_it(self):
        message = _FakeMessage(self.channel, author_bot=True)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 0)

    async def test_it_leaves_discords_own_notices_alone(self):
        message = _FakeMessage(self.channel, kind="pins_add")
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 0)

    async def test_unlocking_forgets_the_lock_the_same_moment(self):
        await server_control.unlock_channel(self.guild, self.channel, Actor())
        message = _FakeMessage(self.channel, author_id=7)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 0, "the row is away, so the removal stops")

    async def test_a_server_that_turned_moderation_off_is_not_policed(self):
        from cogs.server_control import ServerControl

        cog = ServerControl(bot=_FakeBot(enabled=False))
        message = _FakeMessage(self.channel, author_id=7)
        await cog.on_message(message)
        self.assertEqual(message.deleted, 0)

    async def test_a_message_in_a_channel_it_does_not_manage_is_left_alone(self):
        other = Channel(2, "memes")
        self.guild.channels.append(other)
        other.guild = self.guild
        message = _FakeMessage(other, author_id=7)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 0)

    async def test_a_direct_message_is_not_a_channel_anybody_locked(self):
        message = _FakeMessage(SimpleNamespace(id=3, name="dm"), author_id=7)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 0)


class TheOverwritesWeWrote(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The record of what this bot wrote — the half the ledger never had.

    A ledger says *what was here before a lock*; this says *what we put here*, written
    at the moment of the write, so an overwrite of ours is recognised by its own
    history after the command that made it has been rewritten. That history is not
    hypothetical: `/unlock` used to open rooms by **allowing** `@everyone` to talk,
    the command was rewritten to remove the overwrite instead, and the rooms it had
    already touched kept the allow with nothing anywhere saying whose it was.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _guild(self, *channels):
        guild = Guild(channels=list(channels))
        for channel in channels:
            channel.guild = guild
        return guild

    async def test_a_lock_writes_down_what_it_wrote(self):
        channel = Channel(1, "quiet")
        guild = self._guild(channel)

        await server_control.lock_channel(guild, channel, Actor())

        rows = await db.overwrite_rows(guild.id)
        self.assertEqual(len(rows), 1, "the lock left no record of its own write")
        channel_id, target_id, allow, deny, reason, wrote_at = rows[0]
        self.assertEqual((channel_id, target_id), (1, guild.default_role.id))
        self.assertTrue(deny & Bits.NAMES["send_messages"])
        self.assertEqual(allow, 0)
        self.assertIn("lockdown", reason)
        self.assertGreater(wrote_at, 0, "the record has no date to read back")

    async def test_taking_an_overwrite_back_is_recorded_as_a_removal(self):
        """A row of its own rather than a delete: *taken back* is not *never ours*."""
        channel = Channel(1, "quiet")
        guild = self._guild(channel)
        await server_control.lock_channel(guild, channel, Actor())

        await server_control.unlock_channel(guild, channel, Actor())

        self.assertEqual(await db.overwrite_rows(guild.id), [])
        self.assertNotIn(guild.default_role, channel.overwrites)
        self.assertEqual(await server_control.recorded_overwrites(guild, [channel]), [])

    async def test_the_record_is_read_against_the_channel_not_trusted(self):
        """An overwrite edited since is no longer the thing this bot put there."""
        channel = Channel(1, "quiet")
        guild = self._guild(channel)
        await server_control.lock_channel(guild, channel, Actor())
        self.assertEqual(len(await server_control.recorded_overwrites(guild, [channel])), 1)

        channel.overwrites[guild.default_role] = Overwrite(
            deny=Bits.NAMES["send_messages"], allow=1 << 10
        )

        self.assertEqual(
            await server_control.recorded_overwrites(guild, [channel]), [],
            "an edited overwrite was still reported as this bot's write",
        )

    async def test_a_recorded_grant_is_taken_back_where_that_takes_nothing_away(self):
        channel = Channel(1, "general")
        guild = self._guild(channel)
        channel.overwrites[guild.default_role] = Overwrite(allow=Bits.NAMES["send_messages"])
        await db.record_overwrites(
            guild.id,
            [(1, guild.default_role.id, Bits.NAMES["send_messages"], 0, "unlock by a person", 0)],
            at=1.0,
        )

        done, why = await server_control.release_recorded_overwrite(guild, channel, Actor())

        self.assertTrue(done, why)
        self.assertNotIn(guild.default_role, channel.overwrites)

    async def test_a_recorded_grant_that_speaks_for_the_room_is_left_alone(self):
        """The floor test, per permission: taking it back would remove a permission."""
        channel = Channel(1, "general")
        guild = self._guild(channel)
        guild.default_role.permissions = Perms(manage_channels=False, send_messages=False)
        channel.overwrites[guild.default_role] = Overwrite(allow=Bits.NAMES["send_messages"])

        done, why = await server_control.release_recorded_overwrite(guild, channel, Actor())

        self.assertFalse(done, "a room's only way to talk was taken away")
        self.assertIn("only", why)
        self.assertIn(guild.default_role, channel.overwrites)

    async def test_a_recorded_deny_is_handed_to_the_ledger_rather_than_removed(self):
        """Removing a deny would *grant* what it held — so it becomes undoable instead."""
        channel = Channel(1, "quiet")
        guild = self._guild(channel)
        await db.record_overwrites(
            guild.id,
            [(1, guild.default_role.id, 0, Bits.NAMES["send_messages"], "lockdown by a person", 0)],
            at=1.0,
        )
        channel.overwrites[guild.default_role] = Overwrite(deny=Bits.NAMES["send_messages"])

        released, why = await server_control.release_recorded_overwrite(guild, channel, Actor())
        adopted, detail = await server_control.adopt_recorded_overwrite(guild, channel, Actor())

        self.assertFalse(released, "a deny was removed as if it were a grant")
        self.assertIn("denies something", why)
        self.assertTrue(adopted, detail)
        rows = await db.lockdown_rows(guild.id)
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(
            rows[0][1], "",
            "the row claims to know what was here before the write, which nobody does",
        )

        # And the row it wrote is the undo `/unlock` needs: an empty previous reads
        # as *there was nothing here*, so the lock is taken back by **removing** the
        # overwrite rather than by putting one back.
        report = await server_control.unlock_channel(guild, channel, Actor())
        self.assertTrue(report.ok, report.detail)
        self.assertNotIn(guild.default_role, channel.overwrites)

    async def test_the_record_recognises_a_write_today_s_code_never_writes(self):
        """What the record is for: a write whose shape today's code never writes.

        The record is read on its own — no audit log, no fingerprint that today's
        `looks_like_our_lock` would recognise — because the row is the proof.
        """
        channel = Channel(1, "general")
        guild = self._guild(channel)
        reactions = Bits.NAMES["add_reactions"]
        channel.overwrites[guild.default_role] = Overwrite(allow=reactions)

        self.assertFalse(server_control.looks_like_our_lock(channel.overwrites[guild.default_role]))
        self.assertFalse(server_control.is_stray_unlock(channel.overwrites[guild.default_role]))
        await db.record_overwrites(
            guild.id,
            [(1, guild.default_role.id, reactions, 0, "a command since rewritten", 0)],
            at=1.0,
        )

        found = await server_control.recorded_overwrites(guild, [channel])

        self.assertEqual(len(found), 1, "the record did not recognise its own write")
        _channel, allow, deny, reason, _at = found[0]
        self.assertTrue(allow.add_reactions)
        self.assertEqual(deny.value, 0)
        self.assertIn("rewritten", reason)


class TheStaffTalkThroughAWholeServerLockdown(
    TempDatabaseMixin, unittest.IsolatedAsyncioTestCase
):
    """`/lockdown-server` stops the server; it must not stop the people running it.

    A channel permission cannot stop an administrator — that is why the sweep
    exists at all — so a whole-server lockdown written as *"nobody talks"* is a
    lockdown whose staff cannot coordinate through the raid it was declared for.
    The server-wide command is therefore the one lock that leaves administrators
    and the owner talking, and `/lockdown` on a single channel is the opposite
    decision: one room, held for everyone, which is what the screenshot this
    feature came from asks for.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()
        from cogs.server_control import ServerControl

        self.cog = ServerControl(bot=_FakeBot(enabled=True))
        self.channel = Channel(1, "general")
        self.guild = Guild(channels=[self.channel])

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def test_only_the_server_wide_lock_leaves_rank_alone(self):
        self.assertTrue(server_control.staff_exempt("server"))
        for other in ("channel", "", None):
            self.assertFalse(server_control.staff_exempt(other), repr(other))

    async def test_each_command_records_which_kind_of_lock_it_is(self):
        await server_control.lock_channel(self.guild, self.channel, Actor())
        self.assertEqual(
            await server_control.lock_scope(self.guild.id, self.channel), "channel"
        )

        second = Channel(2, "memes")
        second.guild = self.guild
        self.guild.channels.append(second)
        await server_control.lockdown(self.guild, Actor())
        self.assertEqual(await server_control.lock_scope(self.guild.id, second), "server")
        self.assertEqual(
            await server_control.lock_scope(self.guild.id, self.channel), "channel",
            "the server-wide lockdown rewrote the lock that was already there",
        )

    async def test_an_administrators_message_stays_in_a_whole_server_lockdown(self):
        await server_control.lockdown(self.guild, Actor())
        admin = _FakeMember(author_id=7, administrator=True)
        message = _FakeMessage(self.channel, author=admin)
        await self.cog.on_message(message)
        self.assertEqual(
            message.deleted, 0, "an administrator was swept out of a whole-server lockdown"
        )
        self.assertEqual(admin.dms, [], "a message that stayed is not one to warn about")

    async def test_the_owner_stays_and_is_never_timed_out(self):
        self.guild.owner_id = 42
        await server_control.lockdown(self.guild, Actor())
        owner = _FakeMember(author_id=42)
        for _ in range(server_control.SPAM_LIMIT):
            await self.cog.on_message(_FakeMessage(self.channel, author=owner))
        self.assertEqual(owner.timeouts, [], "the owner was timed out by a whole-server lockdown")
        self.assertEqual(owner.dms, [])

    async def test_anybody_else_is_still_removed_and_warned(self):
        await server_control.lockdown(self.guild, Actor())
        member = _FakeMember(author_id=7)
        message = _FakeMessage(self.channel, author=member)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 1, "a member was left talking in a locked server")
        self.assertEqual(len(member.dms), 1, "the member was not warned")

    async def test_a_staff_burst_in_a_whole_server_lockdown_earns_no_ladder(self):
        """Nothing they send counts toward a burst — so it is not the next
        person's window either."""
        await server_control.lockdown(self.guild, Actor())
        admin = _FakeMember(author_id=7, administrator=True)
        for _ in range(server_control.SPAM_LIMIT * 2):
            await self.cog.on_message(_FakeMessage(self.channel, author=admin))
        self.assertEqual(admin.timeouts, [])
        self.assertIsNone(
            server_control.note_locked_message(self.guild.id, self.channel.id, 7, now=0.0),
            "the sweep counted an administrator's messages toward a burst",
        )

    async def test_one_channels_lock_still_holds_the_staff(self):
        await server_control.lock_channel(self.guild, self.channel, Actor())
        admin = _FakeMember(author_id=7, administrator=True)
        message = _FakeMessage(self.channel, author=admin)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 1, "a per-channel lock is meant to hold everyone")
        self.assertEqual(len(admin.dms), 1)

    async def test_a_thread_is_held_by_its_parents_scope(self):
        await server_control.lockdown(self.guild, Actor())
        thread = SimpleNamespace(id=99, parent_id=1)
        self.assertEqual(await server_control.lock_scope(self.guild.id, thread), "server")
        self.assertTrue(await server_control.is_locked(self.guild.id, thread))

    async def test_a_lock_written_before_the_column_holds_everyone(self):
        """A ledger from before this pass has no scope, and a missing answer must
        never loosen a lock: the boot's upgrade adds the column and every old row
        reads as the strict lock."""
        import sqlite3

        from cogs.server_control import ServerControl

        guild = Guild(channels=[Channel(1, "general")])
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DROP TABLE lockdown_state")
        conn.execute(
            "CREATE TABLE lockdown_state (guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "previous TEXT NOT NULL DEFAULT '', locked_at REAL NOT NULL DEFAULT 0, "
            "locked_by INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, channel_id))"
        )
        conn.execute(
            "INSERT INTO lockdown_state (guild_id, channel_id, previous, locked_at, locked_by) "
            "VALUES (?, 1, '', 0, 0)",
            (guild.id,),
        )
        conn.commit()
        conn.close()

        await db.setup()
        server_control.forget_lock_cache()
        self.assertEqual(
            await server_control.lock_scope(guild.id, guild.channels[0]),
            "channel",
            "an old row was read as a lock that exempts rank",
        )
        admin = _FakeMember(author_id=7, administrator=True)
        message = _FakeMessage(guild.channels[0], author=admin)
        await ServerControl(bot=_FakeBot(enabled=True)).on_message(message)
        self.assertEqual(message.deleted, 1, "the upgraded old row stopped holding the staff")

    async def test_a_restored_ledger_heals_without_a_boot(self):
        """``utils.cloud_sync`` replaces ``bot.db`` with ``os.replace`` under a
        running process, so the file can be older than the code reading it. The
        reader must add the missing column itself — no ``db.setup()`` here — or
        the sweep answers as if nothing were locked and stops enforcing.
        """
        import sqlite3

        from cogs.server_control import ServerControl

        guild = Guild(channels=[Channel(1, "general")])
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DROP TABLE lockdown_state")
        conn.execute(
            "CREATE TABLE lockdown_state (guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "previous TEXT NOT NULL DEFAULT '', locked_at REAL NOT NULL DEFAULT 0, "
            "locked_by INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, channel_id))"
        )
        conn.execute(
            "INSERT INTO lockdown_state (guild_id, channel_id, previous, locked_at, locked_by) "
            "VALUES (?, 1, '', 0, 0)",
            (guild.id,),
        )
        conn.commit()
        conn.close()

        # Deliberately no ``await db.setup()``: this is the older, restored file.
        server_control.forget_lock_cache()
        self.assertEqual(
            await server_control.lock_scope(guild.id, guild.channels[0]),
            "channel",
            "an older ledger was not brought up to the code's shape on the read",
        )
        admin = _FakeMember(author_id=7, administrator=True)
        message = _FakeMessage(guild.channels[0], author=admin)
        await ServerControl(bot=_FakeBot(enabled=True)).on_message(message)
        self.assertEqual(message.deleted, 1, "the healed old row stopped holding the staff")


class TheBurstEarnsATimeout(unittest.TestCase):
    """Four messages are somebody ignoring a notice; five are a flood.

    The rules are pure and the clock is injected, so every rung of the ladder and
    every edge of the window is a test rather than a stopwatch.
    """

    def setUp(self):
        server_control.forget_spam()

    def tearDown(self):
        server_control.forget_spam()

    def _burst(self, start):
        """``SPAM_LIMIT`` messages all inside one window; the strike, if any."""
        strike = None
        for _ in range(server_control.SPAM_LIMIT):
            strike = server_control.note_locked_message(1, 2, 3, now=start)
        return strike

    def test_the_messages_below_the_limit_are_not_a_burst(self):
        for index in range(server_control.SPAM_LIMIT - 1):
            self.assertIsNone(
                server_control.note_locked_message(1, 2, 3, now=index * 0.1)
            )

    def test_the_message_that_reaches_the_limit_is_a_strike(self):
        for index in range(server_control.SPAM_LIMIT - 1):
            server_control.note_locked_message(1, 2, 3, now=index * 0.1)
        strike = server_control.note_locked_message(1, 2, 3, now=1.0)
        self.assertIsNotNone(strike)
        self.assertEqual(strike.messages, server_control.SPAM_LIMIT)
        self.assertEqual(strike.offences, 1)
        self.assertEqual(strike.seconds, server_control.SPAM_TIMEOUTS[0])

    def test_the_ladder_escalates_and_holds_at_its_last_rung(self):
        self.assertEqual(
            [server_control.spam_timeout(offences) for offences in (1, 2, 3, 4, 9)],
            [600, 3600, 86400, 86400, 86400],
        )

    def test_a_burst_an_hour_later_starts_at_the_bottom_again(self):
        later = server_control.SPAM_ESCALATION_SECONDS + 1
        self.assertEqual(self._burst(0.0).offences, 1)
        self.assertEqual(self._burst(60.0).offences, 2, "a second burst inside the hour is worse")
        self.assertEqual(
            self._burst(later * 3).offences, 1, "an hour of quiet is not an escalation"
        )

    def test_messages_outside_the_window_are_never_a_burst(self):
        apart = server_control.SPAM_WINDOW_SECONDS + 1
        for index in range(server_control.SPAM_LIMIT * 3):
            strike = server_control.note_locked_message(1, 2, 3, now=index * apart)
        self.assertIsNone(strike)

    def test_one_persons_burst_does_not_time_out_another(self):
        for _ in range(server_control.SPAM_LIMIT - 1):
            server_control.note_locked_message(1, 2, 3, now=0.0)
        self.assertIsNone(server_control.note_locked_message(1, 2, 4, now=0.1))

    def test_another_channel_has_its_own_window(self):
        for _ in range(server_control.SPAM_LIMIT - 1):
            server_control.note_locked_message(1, 2, 3, now=0.0)
        self.assertIsNone(server_control.note_locked_message(1, 9, 3, now=0.1))

    def test_forgetting_one_server_leaves_the_others_alone(self):
        for _ in range(server_control.SPAM_LIMIT - 1):
            server_control.note_locked_message(1, 2, 3, now=0.0)
            server_control.note_locked_message(7, 2, 3, now=0.0)
        server_control.forget_spam(7)
        self.assertIsNotNone(server_control.note_locked_message(1, 2, 3, now=0.1))
        self.assertIsNone(server_control.note_locked_message(7, 2, 3, now=0.1))


class TheCardSaysWhatTheLockCannotDo(unittest.TestCase):
    """A lock that promises deletion and a timeout while the permissions are
    missing is exactly the claim this feature exists to stop making. Locking needs
    **Manage Channels**, removing needs **Manage Messages**, the timeout needs
    **Moderate Members**, and a bot can hold the first without the others.
    """

    @staticmethod
    def _guild():
        return Guild(channels=[])

    @staticmethod
    def _channel(**perms):
        return SimpleNamespace(
            name="general", permissions_for=lambda _me: Perms(manage_channels=True, **perms)
        )

    def test_a_channel_that_can_do_both_says_nothing_extra(self):
        from cogs.server_control import _lock_warnings

        channel = self._channel(manage_messages=True, moderate_members=True)
        self.assertEqual(_lock_warnings(self._guild(), channel), "")

    def test_a_channel_without_manage_messages_says_the_removal_cannot_happen(self):
        from cogs.server_control import _lock_warnings

        channel = self._channel(manage_messages=False, moderate_members=True)
        self.assertIn("Manage Messages", _lock_warnings(self._guild(), channel))

    def test_a_channel_without_moderate_members_says_the_timeout_cannot_happen(self):
        from cogs.server_control import _lock_warnings

        channel = self._channel(manage_messages=True, moderate_members=False)
        self.assertIn("Moderate Members", _lock_warnings(self._guild(), channel))

    def test_both_missing_is_both_sentences(self):
        from cogs.server_control import _lock_warnings

        said = _lock_warnings(self._guild(), self._channel())
        self.assertIn("Manage Messages", said)
        self.assertIn("Moderate Members", said)

    def test_a_channel_it_could_not_read_is_not_a_warning(self):
        from cogs.server_control import _lock_warnings

        self.assertEqual(_lock_warnings(self._guild(), SimpleNamespace(name="general")), "")


class TheCommandAndTheListenerAreOnePath(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The report this pass came from, driven end to end.

    `/lockdown` from the screenshot, then the administrator who ran it typing —
    and the message is gone. Everything else in this file tests one half; this
    drives the real command callback, the real ledger and the real listener over
    the same fakes, which is the shape the beta server sees.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()
        from cogs.server_control import ServerControl

        self.cog = ServerControl(bot=_FakeBot(enabled=True))
        self.channel = Channel(1, "premier")
        # This bot holds both permissions, so the card promises removal without the
        # warning and the fakes do not decide the outcome.
        self.channel.permissions_for = lambda _me: Perms(
            manage_channels=True, manage_messages=True, moderate_members=True
        )
        self.guild = Guild(channels=[self.channel])
        self.interaction = _FakeInteraction(self.guild, self.channel)

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _said(self) -> str:
        payloads = self.interaction.response.messages + self.interaction.followup.messages
        return " ".join(_payload_text(payload) for payload in payloads)

    async def test_the_lock_command_records_the_channel_the_listener_then_clears(self):
        from cogs.server_control import ServerControl

        await ServerControl.lockdown.callback(self.cog, self.interaction)
        said = self._said()
        self.assertIn("deleted", said, "the card does not say what the lock does")
        self.assertIn("administrators included", said)
        self.assertIn("<@42>", said, "the card does not name who locked it")
        self.assertEqual([row[0] for row in await db.lockdown_rows(self.guild.id)], [1])

        # The administrator who ran the lock, typing under its card: the message is
        # removed and the warning reaches them in their own DMs.
        admin = _FakeMember(author_id=42)
        message = _FakeMessage(self.channel, author=admin)
        await self.cog.on_message(message)
        self.assertEqual(message.deleted, 1, "the message the screenshot is about stayed")
        self.assertEqual(len(admin.dms), 1, "the sender was not warned")
        self.assertIn("locked", _payload_text(admin.dms[0]))

        # The unlock goes through its own callback, and the removal stops with it.
        await ServerControl.unlock.callback(self.cog, self.interaction)
        again = _FakeMessage(self.channel, author=admin)
        await self.cog.on_message(again)
        self.assertEqual(again.deleted, 0)
        self.assertEqual(len(admin.dms), 1, "a message that was kept is not warned about")
        self.assertEqual(await db.lockdown_rows(self.guild.id), [])

    async def test_the_hide_option_hides_the_channel_and_the_card_says_so(self):
        """The option is the answer to *my private channel is still visible*: the
        `@everyone` deny is the half a role's own allow can beat, so the lock has
        to be asked to deny `View Channel` on every overwrite that grants it."""
        from cogs.server_control import ServerControl

        await ServerControl.lockdown.callback(self.cog, self.interaction, True)
        said = self._said().lower()
        self.assertIn("hidden", said, "the card does not say the channel was hidden")
        self.assertIn("unlock", said, "the card does not say how to bring it back")
        self.assertIs(self.channel.overwrites[self.guild.default_role].view_channel, False)


class TheHideHasThreeWaysIn(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """`/hide` driven through its real callback, for the two ways in that are new.

    The complaint this came from is not a permission and not a deny that failed to
    land: *"it doesn't have the category that I wanna hide or all the channels"*.
    The picker is one long list ordered by position across the whole server, so a
    server with twenty-odd rooms puts its **categories** past the bottom of what
    anybody scrolls to, and the one a reader wanted reads as missing. So there are
    three ways to say what to hide — the picker (`target`), a name (`name`) and all
    of them (`everything`) — and **one at a time**: two answers to one question is a
    command that cannot say which one it followed, and that has to be a refusal
    before anything is written rather than a card implying work happened.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()
        from cogs.server_control import ServerControl

        self.everyone = Role("@everyone")
        # Named the way a server names one, because the point of the name path is
        # that the decoration is not something a person types.
        self.category = Channel(1, "📁・archive", kind="category")
        self.inside = Channel(2, "notes")
        self.inside.category = self.category
        self.category.channels = [self.inside]
        self.loose = Channel(3, "general")
        self.guild = Guild(channels=[self.category, self.inside, self.loose])
        # The person who owns this server, which is the only one `/hide` answers.
        self.guild.owner_id = 42
        self.guild.default_role = self.everyone
        self.cog = ServerControl(bot=_FakeBot(enabled=True))
        self.interaction = _FakeInteraction(self.guild, self.loose)

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _said(self) -> str:
        payloads = self.interaction.response.messages + self.interaction.followup.messages
        return " ".join(_payload_text(payload) for payload in payloads)

    async def _hide(self, target=None, name=None, everything=False, show=False):
        from cogs.server_control import ServerControl

        await ServerControl.hide.callback(
            self.cog, self.interaction, target, name, everything, show
        )

    async def test_everything_takes_the_categories_with_the_rooms(self):
        await self._hide(everything=True)
        for channel in (self.category, self.inside, self.loose):
            self.assertIs(
                channel.overwrites[self.everyone].view_channel, False, channel.name
            )
        self.assertIn(
            "everything:True show:True",
            self._said(),
            "the card does not say how to put a whole server back",
        )

    async def test_a_name_hides_the_category_the_picker_would_not_reach(self):
        """The decorated name a server actually uses, read by the binder's own rule."""
        await self._hide(name="archive")
        self.assertIs(self.category.overwrites[self.everyone].view_channel, False)
        self.assertIs(
            self.inside.overwrites[self.everyone].view_channel,
            False,
            "a category takes its rooms with it through a name as well as a picker",
        )
        self.assertNotIn(self.everyone, self.loose.overwrites)
        self.assertIn("name: archive show:True", self._said())

    async def test_two_ways_in_at_once_are_refused_without_hiding_anything(self):
        await self._hide(self.loose, everything=True)
        self.assertIn("One way in at a time", self._said())
        self.assertEqual(self.loose.overwrites, {}, "a refused command changed a room")
        self.assertEqual(
            await db.hide_rows(self.guild.id), [], "a refused command wrote a row"
        )

    async def test_no_way_in_at_all_is_refused_before_anything_happens(self):
        await self._hide()
        self.assertIn("Say what to hide", self._said())
        self.assertEqual(await db.hide_rows(self.guild.id), [])

    async def test_a_name_nothing_answers_to_is_refused(self):
        await self._hide(name="not-a-channel")
        self.assertIn("Nothing here is called", self._said())
        self.assertEqual(await db.hide_rows(self.guild.id), [])


class TheLockedChannelsAreCached(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The question is asked on every message, so the answer cannot be a query."""

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()

    async def test_a_burst_of_messages_reads_the_ledger_once(self):
        guild = Guild(channels=[Channel(1, "general")])
        await server_control.lock_channel(guild, guild.channels[0], Actor())
        real = server_control.db.lockdown_locks
        reads = []

        async def counted(guild_id):
            reads.append(guild_id)
            return await real(guild_id)

        with mock.patch.object(server_control.db, "lockdown_locks", new=counted):
            for _ in range(5):
                self.assertTrue(await server_control.is_locked(guild.id, guild.channels[0]))
        self.assertEqual(len(reads), 1, "the cache answered the other four")

    async def test_locking_and_unlocking_forget_what_was_cached(self):
        guild = Guild(channels=[Channel(1, "general")])
        self.assertFalse(await server_control.is_locked(guild.id, guild.channels[0]))
        await server_control.lock_channel(guild, guild.channels[0], Actor())
        self.assertTrue(await server_control.is_locked(guild.id, guild.channels[0]))
        await server_control.unlock_channel(guild, guild.channels[0], Actor())
        self.assertFalse(await server_control.is_locked(guild.id, guild.channels[0]))


class TheOverwritesThatWereAllowedIn(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """The reported bug, as laws: a deny on `@everyone` alone is not a lock.

    Discord applies a role's and a member's channel overwrite **after**
    `@everyone`'s, so an explicit allow there beats the deny — which is how a
    channel could read as locked and still have people seeing it and typing in it.
    Two halves are pinned here: the lock denies every overwrite that granted the
    permission, and the ledger puts **each** of them back, because an undo that
    restored `@everyone` and forgot the role would leave the channel open the
    moment somebody typed `/unlock`.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    async def test_the_overwrite_it_found_is_kept_and_the_deny_is_added_to_it(self):
        """A private channel's `View Channel=False` is not the lock's to take away.

        The old body sent a fresh overwrite, which Discord stores as *deny these
        three, say nothing about anything else* — so locking a room that had been
        made private handed it back to every member."""
        everyone = Role("@everyone")
        channel = Channel(
            1,
            "private",
            overwrites={
                everyone: Overwrite(
                    deny=Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"]
                )
            },
        )
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await server_control.lock_channel(guild, channel, Actor())
        held = channel.overwrites[everyone]
        self.assertIs(
            held.view_channel, False, "the lock handed a private channel back to the server"
        )
        self.assertIs(held.send_messages, False)
        self.assertIs(held.add_reactions, False)
        self.assertIs(held.create_public_threads, False)

    async def test_a_role_that_had_been_allowed_to_talk_is_denied_too(self):
        normal = Role("Normal", id=7)
        channel = Channel(
            1,
            "general",
            overwrites={
                normal: Overwrite(allow=Bits.NAMES["send_messages"] | Bits.NAMES["add_reactions"])
            },
        )
        guild = Guild(channels=[channel], roles=[normal])
        report = await server_control.lock_channel(guild, channel, Actor())
        held = channel.overwrites[normal]
        self.assertIs(held.send_messages, False, "a role's allow beat the deny")
        self.assertIs(held.add_reactions, False)
        self.assertIsNone(held.view_channel, "an ordinary lock does not hide the channel")
        self.assertEqual(report.evicted, 1, "the card cannot say it closed an allow it did not")

    async def test_a_member_a_ticket_let_in_is_denied_too(self):
        member = _FakeMember(author_id=9)
        channel = Channel(
            1,
            "ticket-raj",
            overwrites={
                member: Overwrite(allow=Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"])
            },
        )
        guild = Guild(channels=[channel], members=[member])
        await server_control.lock_channel(guild, channel, Actor())
        held = channel.overwrites[member]
        self.assertIs(held.send_messages, False, "the member a ticket let in kept talking")
        self.assertIs(held.view_channel, True, "an ordinary lock does not hide the room either")

    async def test_the_bots_own_allow_is_left_alone(self):
        """Not politeness: the card it posts into the room, and the messages it
        removes from it, both need the allow it already had."""
        me = _FakeMember(author_id=424242, bot=True)
        channel = Channel(1, "general", overwrites={me: Overwrite(allow=Bits.NAMES["send_messages"])})
        guild = Guild(channels=[channel], members=[me])
        await server_control.lock_channel(guild, channel, Actor())
        allow, _deny = channel.overwrites[me].pair()
        self.assertTrue(allow.send_messages, "the lock took away my own room to work in")

    def test_a_channel_somebody_can_still_talk_in_is_not_already_locked(self):
        everyone = Role("@everyone")
        normal = Role("Normal", id=7)
        channel = Channel(
            1,
            "quiet",
            overwrites={
                everyone: Overwrite(deny=ALL_LOCKED),
                normal: Overwrite(allow=Bits.NAMES["send_messages"]),
            },
        )
        guild = Guild(channels=[channel], roles=[normal])
        guild.default_role = everyone
        plan = server_control.plan(guild)
        self.assertEqual([held.id for held in plan.change], [1])
        self.assertEqual(plan.already_locked, [], "a lock anybody can type through was reported as held")

    async def test_the_undo_puts_the_allow_it_stripped_back(self):
        normal = Role("Normal", id=7)
        channel = Channel(
            1,
            "general",
            overwrites={
                normal: Overwrite(
                    allow=Bits.NAMES["send_messages"], deny=Bits.NAMES["add_reactions"]
                )
            },
        )
        guild = Guild(channels=[channel], roles=[normal])
        await server_control.lock_channel(guild, channel, Actor())
        self.assertIs(channel.overwrites[normal].send_messages, False)
        report = await server_control.unlock_channel(guild, channel, Actor())
        self.assertTrue(report.ok)
        held = channel.overwrites[normal]
        self.assertIs(held.send_messages, True, "the role's allow was not put back")
        self.assertIs(held.add_reactions, False, "a deny the lock found was dropped by the undo")
        self.assertIsNone(held.view_channel)

    async def test_a_row_written_before_this_pass_still_restores_the_everyone_overwrite(self):
        """One ``allow:deny`` string can only ever have been about `@everyone`."""
        everyone = Role("@everyone")
        channel = Channel(1, "quiet", overwrites={everyone: Overwrite(deny=ALL_LOCKED)})
        guild = Guild(channels=[channel])
        guild.default_role = everyone
        await db.save_lockdown(guild.id, [(1, f"{Bits.NAMES['send_messages']}:0")], 5, 0)
        report = await server_control.unlock_channel(guild, channel, Actor())
        self.assertTrue(report.ok)
        held = channel.overwrites[everyone]
        self.assertIs(held.send_messages, True, "an old row stopped restoring")
        self.assertIsNone(held.add_reactions, "the row was read as more than the one overwrite")


class TheHiddenLock(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    """`/lockdown hide:True`: the same lock, with the channel off the list.

    The report begins with a private channel the normal role could still see —
    `@everyone view_channel=False` and an allow for the role, which is the one
    overwrite a deny on `@everyone` cannot answer. A hide denies `View Channel`
    everywhere it is granted, and spares the two overwrites the room needs to be
    openable again: the bot's own, and the people who can manage channels. Their
    sight is spared and their voice is not — an ordinary lock still holds them.
    """

    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def tearDown(self):
        self.release_temp_database()
        server_control.forget_lock_cache()
        server_control.forget_spam()

    def _guild(self, channel, *roles):
        guild = Guild(channels=[channel], roles=list(roles))
        guild.default_role = Role("@everyone")
        return guild

    async def test_it_hides_the_channel_from_the_role_that_was_allowed_to_see_it(self):
        normal = Role("Normal", id=7)
        manager = Role("Moderator", id=8, manage_channels=True)
        channel = Channel(
            1,
            "premier",
            overwrites={
                normal: Overwrite(
                    allow=Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"]
                ),
                manager: Overwrite(
                    allow=Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"]
                ),
            },
        )
        guild = self._guild(channel, normal, manager)
        await server_control.lock_channel(guild, channel, Actor(), hide=True)
        self.assertIs(channel.overwrites[guild.default_role].view_channel, False)
        self.assertIs(
            channel.overwrites[normal].view_channel, False, "the normal role still sees it"
        )
        self.assertIs(channel.overwrites[normal].send_messages, False, "the normal role can still talk")
        self.assertIs(
            channel.overwrites[manager].view_channel,
            True,
            "the people who can open it again lost sight of it",
        )
        self.assertIs(
            channel.overwrites[manager].send_messages, False, "a keeper could still talk through it"
        )

    async def test_the_undo_gives_every_bit_of_visibility_back(self):
        normal = Role("Normal", id=7)
        channel = Channel(
            1,
            "premier",
            overwrites={
                normal: Overwrite(allow=Bits.NAMES["view_channel"] | Bits.NAMES["send_messages"])
            },
        )
        guild = self._guild(channel, normal)
        await server_control.lock_channel(guild, channel, Actor(), hide=True)
        report = await server_control.unlock_channel(guild, channel, Actor())
        self.assertTrue(report.ok)
        self.assertNotIn(
            guild.default_role, channel.overwrites,
            "the hide left an overwrite the channel never had",
        )
        self.assertIs(channel.overwrites[normal].view_channel, True)
        self.assertIs(channel.overwrites[normal].send_messages, True)

    async def test_the_bot_keeps_its_own_sight_of_a_hidden_channel(self):
        me = _FakeMember(author_id=424242, bot=True)
        channel = Channel(1, "premier", overwrites={me: Overwrite(allow=Bits.NAMES["view_channel"])})
        guild = Guild(channels=[channel], members=[me])
        await server_control.lock_channel(guild, channel, Actor(), hide=True)
        allow, _deny = channel.overwrites[me].pair()
        self.assertTrue(allow.view_channel, "a hidden channel my own reader cannot see")

    async def test_a_hidden_channel_is_a_locked_channel_too(self):
        guild = self._guild(Channel(1, "premier"))
        report = await server_control.lock_channel(guild, guild.channels[0], Actor(), hide=True)
        self.assertTrue(report.ok)
        self.assertEqual(await server_control.locked_channel_ids(guild.id), frozenset({1}))
        self.assertEqual(await server_control.lock_scope(guild.id, guild.channels[0]), "channel")


class TheSlowmode(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database()
        await db.setup()

    def tearDown(self):
        self.release_temp_database()

    async def test_it_sets_the_value_on_every_text_channel_and_can_turn_it_off(self):
        channels = [Channel(1, "a"), Channel(2, "b"), Channel(3, "voice", kind="voice")]
        guild = Guild(channels=channels)
        report = await server_control.set_slowmode(guild, 30, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 2)
        self.assertEqual([channel.slowmode_delay for channel in channels], [30, 30, 0])
        await server_control.set_slowmode(guild, 0, Actor())
        self.assertEqual(channels[0].slowmode_delay, 0, "0 is how slowmode is turned off")

    async def test_a_value_over_discords_ceiling_is_clamped_not_refused(self):
        channel = Channel(1, "a")
        guild = Guild(channels=[channel])
        report = await server_control.set_slowmode(guild, 999999, Actor())
        self.assertTrue(report.ok)
        self.assertEqual(channel.slowmode_delay, 21600)

    async def test_channels_it_cannot_manage_are_counted_rather_than_silently_skipped(self):
        guild = Guild(channels=[Channel(1, "a"), Channel(2, "b", manage=False)])
        report = await server_control.set_slowmode(guild, 5, Actor())
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.blocked, 1)

    async def test_the_state_reading_reports_what_is_locked_and_what_would_change(self):
        """The plan half of this reading was dead until the overwrite reader was
        fixed: `_denies` asked a `PermissionOverwrite` for an ``allow``/
        ``deny`` attribute it does not have, so *already locked* was never true on
        a real server and this law used to pin that (``already_locked == 0``
        **after** a lockdown, which is a claim that a lockdown changed nothing).
        The straight answer is the one asserted now.
        """
        guild = Guild(channels=[Channel(1, "general"), Channel(2, "memes")])
        self.assertEqual((await server_control.state(guild))["locked"], 0)
        await server_control.lockdown(guild, Actor())
        reading = await server_control.state(guild)
        self.assertEqual(reading["locked"], 2)
        self.assertEqual([channel["name"] for channel in reading["channels"]], ["general", "memes"])
        self.assertEqual(
            reading["counts"]["already_locked"], 2,
            "a lockdown is not reported as having left work to do",
        )
        self.assertEqual(reading["counts"]["change"], 0)


class TheReport(unittest.TestCase):
    """A report that cannot say what happened is a log line nobody can act on."""

    def test_a_failed_report_says_why_rather_than_counting_zero(self):
        from utils.server_control import Report

        report = Report(action="lockdown", ok=False, detail="I need Manage Channels there.")
        self.assertEqual(report.sentence(), "I need Manage Channels there.")

    def test_a_successful_report_counts_what_it_did_and_what_it_could_not(self):
        from utils.server_control import Report

        report = Report(action="lockdown", ok=True, changed=12, already=2, blocked=3)
        said = report.sentence()
        self.assertIn("12", said)
        self.assertIn("2 were already locked", said)
        self.assertIn("3 could not be managed", said)
        self.assertFalse(report.to_dict()["failures"])

    def test_a_report_says_when_the_lock_closed_an_allow_as_well(self):
        """A lock that had to take an allow away is a different event from one
        that only had to write its own deny, and the panel says which happened."""
        from utils.server_control import Report

        report = Report(action="lockdown", ok=True, changed=1, evicted=2)
        self.assertIn("2 overwrite(s)", report.sentence())
        self.assertEqual(report.to_dict()["evicted"], 2)
        self.assertNotIn("overwrite(s)", Report(action="lockdown", ok=True, changed=1).sentence())


if __name__ == "__main__":
    unittest.main()
