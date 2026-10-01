"""Alone Time: the three layers, tested apart and then together.

The feature's claim is the strongest sentence in this bot — *nobody else can be
inside this room, not an administrator and not the owner* — and it is carried by
three mechanisms that fail differently, which is why the tests are in three
groups rather than one:

* **The rules** are pure and are checked exhaustively, because they are the part
  somebody would argue about: who a room admits, how many seats its cap needs,
  which readings are allowed to release a lock and which are not.
* **The ledger and the engine** run against stand-in channels. The two laws that
  matter are copied from ``utils/server_control``'s own tests: the undo is
  written **before** the first permission change, and a release puts back exactly
  what was there — deleting the overwrite when the room had none, because
  *allowing* it instead hands out a permission nobody granted.
* **The sweep and the commands** drive the cog directly, with the callbacks
  discord.py would call, because the half that needs a gateway can only be
  checked by handing it one.

What is deliberately pinned here rather than left to a live check: Discord
refusing a move is *reported and never pretended away*, a failed write leaves the
lock ledger empty rather than claiming a room that is not locked, and a rename
made after the lock is not clobbered by the release.
"""
import re
import time
import unittest
from unittest import mock

import discord

from tests.support import TempDatabaseMixin
from utils import alonetime, db, features, permissions

# --------------------------------------------------------------------------- #
# Stand-ins: the smallest objects the code under test actually reads
# --------------------------------------------------------------------------- #
class _Bits:
    """A bitfield with the one thing the ledger stores — its value."""

    def __init__(self, value=0):
        self.value = int(value)

    def __getattr__(self, _name):
        return False


class _Overwrite:
    """A permission overwrite **without** ``pair()``, which is the shape that is not
    the library's.

    A real ``discord.PermissionOverwrite`` has no ``allow``/``deny`` attributes and
    is read with ``pair()``; this exists so the encoder's fallback path stays
    covered, and the round trip below the encoder's own test uses the real class
    through ``from_pair`` so the path production takes is the one exercised.
    """

    def __init__(self, *, allow=0, deny=0):
        self.allow = _Bits(allow)
        self.deny = _Bits(deny)


class _Perms:
    """A permission set: everything named, everything else False."""

    def __init__(self, **values):
        for name, value in values.items():
            setattr(self, name, value)

    def __getattr__(self, _name):
        return False


class _Role:
    def __init__(self, rid, name="guest", members=()):
        self.id = rid
        self.name = name
        self.members = list(members)


class _VoiceState:
    def __init__(self, channel=None):
        self.channel = channel


class _Sent:
    """The message-shaped thing a stand-in's ``send`` answers with.

    ``alonetime.remember_request`` is keyed by the delivered message id, so a
    stand-in that answered ``None`` would make the request untraceable and the
    test would be proving its own stub, not the feature.
    """

    _next = 800000

    def __init__(self):
        _Sent._next += 1
        self.id = _Sent._next


class _Member:
    def __init__(self, uid, name="someone", *, guild=None, bot=False, roles=(), perms=None):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot
        self.guild = guild
        self.roles = list(roles)
        self.guild_permissions = perms or _Perms()
        self.voice = _VoiceState()
        self.moves = []
        #: Every DM the bot sent this member, in order — the request card is the
        #: only thing this feature ever DMs, and a test has to be able to read it —
        #: plus their ids, which is what a press on the card carries.
        self.dms = []
        self.dm_ids = []

    def __repr__(self):
        return self.display_name

    async def move_to(self, target, reason=""):
        self.moves.append(target)
        self.voice.channel = target

    async def send(self, **kwargs):
        """A Messageable, so ``style.send_panel(holder, card)`` lands as a DM."""
        self.dms.append(kwargs)
        message = _Sent()
        self.dm_ids.append(message.id)
        return message


class _Channel:
    """A voice channel, with the two writes a lock makes and a place to send."""

    def __init__(self, guild, cid, name="Lobby", *, type_="voice", manage=True):
        self.guild = guild
        self.id = cid
        self.name = name
        self.type = type_
        self.overwrites = {}
        self.user_limit = 0
        self.members = []
        self.sent = []
        #: The ids of what was posted, so a test can press a button on the card
        #: that was actually sent (a notice's *Ask to join* resolves its room by
        #: the message id, and a stub that answered ``None`` would be untestable).
        self.sent_ids = []
        self._manage = manage
        #: One journal for *both* halves of a lock, so a test can prove the
        #: ledger was written before the first permission change — the ordering
        #: law ``utils/server_control`` already lives by.
        self.journal = []

    def permissions_for(self, _member):
        return _Perms(manage_channels=self._manage)

    async def set_permissions(self, target, *, overwrite=None, reason=""):
        self.journal.append(("set_permissions", target, overwrite, reason))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def edit(self, *, name=None, user_limit=None, reason=""):
        self.journal.append(("edit", name, user_limit, reason))
        if name is not None:
            self.name = name
        if user_limit is not None:
            self.user_limit = user_limit

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = _Sent()
        self.sent_ids.append(message.id)
        return message


class _Guild:
    def __init__(self, gid=424242, name="Test Server", *, move_members=True):
        self.id = gid
        self.name = name
        self.default_role = _Role(1, "@everyone")
        self.me = _Member(999, "Meow", bot=True, perms=_Perms(move_members=move_members))
        self.channels = []
        self.members = []
        self.voice_channels = []
        self.stage_channels = []
        self.text_channels = []
        self.afk_channel = None
        self.gone = set()
        self._next_id = 100

    def get_member(self, user_id):
        """The cache lookup ``discord.Guild`` has, which ``style.display_name`` asks first.

        A miss is left as a miss on purpose: the real method does no fetch, and the
        helper's own fallbacks (``fetch_member``, then ``fetch_user``, then
        ``User <id>``) are the path a server with the members intent off takes.
        """
        wanted = int(user_id)
        if wanted == self.me.id:
            return self.me
        for member in self.members:
            if getattr(member, "id", None) == wanted:
                return member
        for channel in self.channels:
            for member in getattr(channel, "members", ()):
                if getattr(member, "id", None) == wanted:
                    return member
        return None

    def add_text_channel(self, name="bot-commands"):
        """A room a notice can land in — the only thing the routing resolver reads."""
        channel = _Channel(self, self._next_id, name, type_="text")
        self._next_id += 1
        self.channels.append(channel)
        self.text_channels.append(channel)
        return channel

    def add_voice_channel(self, name="Lobby", *, manage=True, type_="voice"):
        channel = _Channel(self, self._next_id, name, type_=type_, manage=manage)
        self._next_id += 1
        self.channels.append(channel)
        if type_ == "voice":
            self.voice_channels.append(channel)
        else:
            self.stage_channels.append(channel)
        return channel

    def get_channel(self, channel_id):
        if channel_id in self.gone:
            return None
        return next((c for c in self.channels if c.id == channel_id), None)

    def get_role(self, role_id):
        if role_id == self.default_role.id:
            return self.default_role
        return None


class _Bot:
    def __init__(self, guilds=(), *, module_on=True):
        self.guilds = list(guilds)
        self.user = _Member(999, "Meow", bot=True)
        self._module_on = module_on
        self.voice_clients = []
        #: What ``setup``/``__init__`` registered, so a test can prove the
        #: holder's controls survive a redeploy rather than only the card that
        #: happens to be in front of somebody.
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    def is_module_enabled(self, _guild_id, _module):
        return self._module_on

    async def wait_until_ready(self):
        # Parked for ever: no test depends on a sixty-second timer, and a loop
        # that ran on its own would be a row changing underneath an assertion.
        import asyncio

        await asyncio.Future()


class _Response:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def send_message(self, content=None, *, ephemeral=False, **kwargs):
        # Positional content, exactly as ``discord.InteractionResponse`` takes it:
        # ``interaction_check`` answers a switched-off system with a sentence and
        # nothing else.
        self.sent.append({"content": content, "ephemeral": ephemeral, **kwargs})

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _Interaction:
    def __init__(self, guild, user, *, message=None):
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.user = user
        self.response = _Response()
        self.followup = _Followup()
        self.edits = []
        self.data = {}
        #: The message a button was pressed on, which is how an answer card finds
        #: the request it is about (``alonetime.request_for(message.id)``).
        self.message = message
        #: Where ``style.send_panel`` lands when the target is not an instance of
        #: ``discord.Interaction`` — which a stand-in never is. The view recorded
        #: here is the same card the interaction branch would have sent.
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)

    async def edit_original_response(self, **kwargs):
        # ``style._is_interaction`` asks for this method by name, which is what
        # keeps an edit of a card routed as an edit rather than as a send.
        self.edits.append(kwargs)


def _said(interaction) -> str:
    """Everything an answer to this person carried, as one searchable string.

    Three shapes land in here — an embed, a plain sentence, and a **Components V2
    card**, which has no ``embed`` at all and exists only inside its view. A reader
    that knew the first two reported nothing for a card, which is how an assertion
    about the card's own words could be read as passing against an empty string.
    """
    parts: list[str] = []
    payloads = (
        list(interaction.response.sent)
        + list(interaction.followup.sent)
        + list(getattr(interaction, "sent", []))
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            parts.append(str(payload))
            continue
        view = payload.get("view")
        if view is not None and hasattr(view, "to_components"):
            parts.append(str(view.to_components()))
            continue
        embed = payload.get("embed")
        if embed is None:
            parts.append(str(payload))
            continue
        parts.append(str(getattr(embed, "title", "") or ""))
        parts.append(str(getattr(embed, "description", "") or ""))
        for field in getattr(embed, "fields", None) or []:
            parts.append(f"{getattr(field, 'name', '')} {getattr(field, 'value', '')}")
        footer = getattr(embed, "footer", None)
        parts.append(str(getattr(footer, "text", "") or ""))
    return " ".join(part for part in parts if part)


def _card_text(channel) -> str:
    """What a card posted in a room actually says (Components V2, so not kwargs)."""
    out = []
    for payload in getattr(channel, "sent", []):
        view = payload.get("view") if isinstance(payload, dict) else None
        if view is not None and hasattr(view, "to_components"):
            out.append(str(view.to_components()))
        else:
            out.append(str(payload))
    return " ".join(out)


def _card_of(interaction) -> str:
    """What the last Components V2 card sent *to a person* said.

    Three places a card can land — the response, a follow-up, or an edit of the
    message whose button was pressed — and all three are the same card.
    """
    payloads = (
        list(interaction.followup.sent)
        + list(interaction.response.sent)
        + list(getattr(interaction, "sent", []))
        + list(getattr(interaction.response, "edited", []))
        + list(getattr(interaction, "edits", []))
    )
    for payload in payloads:
        view = payload.get("view") if isinstance(payload, dict) else None
        if view is not None and hasattr(view, "to_components"):
            return str(view.to_components())
    return ""


def _state(channel):
    """A voice-state stand-in: only ``.channel`` is every read this code makes."""
    return type("State", (), {"channel": channel})()


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
class WhoTheRoomAdmits(unittest.TestCase):
    def test_the_holder_is_the_one_person_it_is_held_for(self):
        self.assertEqual(
            alonetime.verdict(holder_id=7, member_id=7), alonetime.ALLOW
        )

    def test_a_named_guest_is_admitted(self):
        self.assertEqual(
            alonetime.verdict(holder_id=7, guests=(8, 9), member_id=9), alonetime.ALLOW
        )

    def test_anybody_holding_a_guest_role_is_admitted(self):
        self.assertEqual(
            alonetime.verdict(
                holder_id=7, guest_roles=(42,), member_id=10, role_ids=(5, 42)
            ),
            alonetime.ALLOW,
        )

    def test_a_stranger_is_turned_away(self):
        self.assertEqual(
            alonetime.verdict(holder_id=7, guests=(8,), member_id=11), alonetime.EVICT
        )

    def test_a_quiet_hours_room_admits_nobody(self):
        """There is no holder, so the room is nobody's to sit in."""
        self.assertEqual(alonetime.verdict(holder_id=0, member_id=11), alonetime.EVICT)

    def test_the_bots_own_connection_is_never_touched(self):
        self.assertEqual(
            alonetime.verdict(holder_id=7, member_id=999, bot=True), alonetime.SKIP
        )

    def test_another_bot_is_left_alone_too(self):
        """``bot=True`` is how a caller says *this one is a bot*.

        ``bot_id`` is this bot's **own** id — the sweep and the room pass both
        hand it their own — so it only ever matches itself. Another bot is skipped
        because the listener reads ``member.bot`` and returns before the rule is
        asked, not because a second id is consulted here.
        """
        self.assertEqual(
            alonetime.verdict(holder_id=7, member_id=555, bot=True), alonetime.SKIP
        )
        self.assertEqual(
            alonetime.verdict(holder_id=7, member_id=999, bot_id=999), alonetime.SKIP
        )
        self.assertEqual(
            alonetime.verdict(holder_id=7, member_id=555, bot_id=999), alonetime.EVICT
        )

    def test_admits_is_the_same_answer_as_the_verdict(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7, guests=(8,))
        self.assertTrue(lock.admits(7))
        self.assertTrue(lock.admits(8))
        self.assertFalse(lock.admits(9))


class HowManySeatsTheCapNeeds(unittest.TestCase):
    def test_a_room_with_no_guests_holds_exactly_its_holder(self):
        self.assertEqual(alonetime.capacity_for(None), 1)

    def test_each_named_guest_is_one_seat(self):
        self.assertEqual(alonetime.capacity_for(None, guests=(8, 9)), 3)

    def test_a_guest_role_is_counted_by_its_members(self):
        role = _Role(42, "staff", members=[_Member(1), _Member(2), _Member(3)])
        self.assertEqual(alonetime.capacity_for(None, guest_roles=(role,)), 4)

    def test_a_role_that_cannot_be_read_still_gets_a_seat(self):
        """Nobody can be let past a full room, so an unreadable role gets one."""
        role = _Role(42, "staff", members=[])
        role.members = None
        self.assertEqual(alonetime.capacity_for(None, guest_roles=(role,)), 2)

    def test_the_cap_never_exceeds_discords_own_ceiling(self):
        self.assertEqual(alonetime.capacity_for(None, guests=tuple(range(200))), 99)

    def test_the_cap_is_never_below_one(self):
        self.assertEqual(alonetime.capacity_for(None, guests=()), 1)


class TheLedger(unittest.TestCase):
    def test_no_overwrite_and_an_empty_overwrite_are_different_facts(self):
        """``""`` means *there was none*; ``"0:0"`` means *one that says nothing*."""
        self.assertEqual(alonetime.encode_overwrite(None), "")
        self.assertIsNone(alonetime.decode_overwrite(""))
        self.assertEqual(alonetime.decode_overwrite("0:0"), (0, 0))

    def test_an_overwrite_round_trips_as_its_two_numbers(self):
        encoded = alonetime.encode_overwrite(_Overwrite(allow=8, deny=16))
        self.assertEqual(encoded, "8:16")
        self.assertEqual(alonetime.decode_overwrite(encoded), (8, 16))

    def test_the_librarys_own_overwrite_is_read_with_pair(self):
        """The shape that broke the ledger: a real overwrite has no ``allow``.

        ``encode_overwrite`` asked for ``.allow``/``.deny``, which a real
        ``discord.PermissionOverwrite`` does not have — ``__slots__`` is ``_values``
        and the pair is what it exposes. Inside a server that answered *none* for
        every room that had an overwrite, so the ledger recorded ``0:0`` and a
        release flattened the room's own settings instead of putting them back.
        """
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(8), discord.Permissions(16)
        )
        self.assertFalse(hasattr(overwrite, "allow"), "the shape the encoder used to assume")
        self.assertEqual(alonetime.encode_overwrite(overwrite), "8:16")

    def test_text_that_is_not_a_pair_is_no_ledger_at_all(self):
        self.assertIsNone(alonetime.decode_overwrite("garbage"))
        self.assertIsNone(alonetime.decode_overwrite("x:y"))

    def test_the_lock_marker_is_put_on_once(self):
        self.assertEqual(alonetime.locked_name("Lobby"), "🔒 Lobby")
        self.assertEqual(alonetime.locked_name("🔒 Lobby"), "🔒 Lobby")

    def test_the_marker_keeps_the_room_inside_discords_length_limit(self):
        self.assertEqual(len(alonetime.locked_name("x" * 200)), 100)

    def test_a_name_is_restored_only_from_the_name_this_feature_set(self):
        self.assertEqual(alonetime.restore_name("Lobby", "🔒 Lobby"), "Lobby")

    def test_a_rename_made_after_the_lock_is_not_clobbered(self):
        """Their decision is newer than ours, and a release is not a rename."""
        self.assertIsNone(alonetime.restore_name("Lobby", "Renamed"))
        self.assertIsNone(alonetime.restore_name("Lobby", "Lobby"))
        self.assertIsNone(alonetime.restore_name("", "🔒 "))


class TheReasonsToLift(unittest.TestCase):
    def _lock(self, **kwargs):
        values = dict(guild_id=1, channel_id=2, holder_id=7, started_at=1000.0)
        values.update(kwargs)
        return alonetime.Lock(**values)

    def test_an_unknown_holder_presence_never_lifts_a_lock(self):
        """A failed reading is not evidence that somebody left."""
        reason = alonetime.release_reason(
            self._lock(), settings=alonetime.Settings(), holder_present=None
        )
        self.assertEqual(reason, "")

    def test_a_holder_who_left_gives_the_room_back(self):
        reason = alonetime.release_reason(
            self._lock(), settings=alonetime.Settings(), holder_present=False
        )
        self.assertIn("left", reason)

    def test_a_room_that_is_gone_is_released_rather_than_held(self):
        reason = alonetime.release_reason(
            self._lock(), settings=alonetime.Settings(), channel_exists=False
        )
        self.assertIn("gone", reason)

    def test_keep_is_the_holder_saying_not_to(self):
        self.assertEqual(
            alonetime.release_reason(
                self._lock(keep=True), settings=alonetime.Settings(), holder_present=False
            ),
            "",
        )
        self.assertEqual(
            alonetime.release_reason(
                self._lock(),
                settings=alonetime.Settings(keep=True),
                holder_present=False,
            ),
            "",
        )

    def test_the_servers_ceiling_ends_a_lock_that_has_run_too_long(self):
        now = 1000.0 + 3 * 3600
        reason = alonetime.release_reason(
            self._lock(),
            settings=alonetime.Settings(max_hours=2),
            holder_present=True,
            now=now,
        )
        self.assertIn("ceiling", reason)

    def test_a_quiet_hours_window_is_not_a_per_room_lock(self):
        lock = self._lock(holder_id=0)
        self.assertTrue(lock.server_wide)
        self.assertEqual(
            alonetime.release_reason(
                lock, settings=alonetime.Settings(), holder_present=False
            ),
            "",
        )


class WhereATurnedAwayMemberGoes(unittest.TestCase):
    def test_back_where_they_came_from_is_the_default(self):
        origin, afk = object(), object()
        self.assertIs(
            alonetime.eviction_target(alonetime.EVICT_ORIGIN, origin=origin, afk=afk),
            origin,
        )

    def test_somebody_with_nowhere_to_go_is_dropped_out_of_voice(self):
        self.assertIsNone(
            alonetime.eviction_target(alonetime.EVICT_ORIGIN, origin=None, afk=None)
        )

    def test_a_server_that_asked_for_disconnect_gets_it(self):
        origin = object()
        self.assertIsNone(
            alonetime.eviction_target(alonetime.EVICT_DISCONNECT, origin=origin, afk=None)
        )

    def test_the_afk_channel_is_the_third_answer_and_not_a_loop(self):
        origin, afk = object(), object()
        self.assertIs(alonetime.eviction_target(alonetime.EVICT_AFK, origin=origin, afk=afk), afk)
        self.assertIsNone(alonetime.eviction_target(alonetime.EVICT_AFK, origin=origin, afk=origin))


class TheSentences(unittest.TestCase):
    def test_a_duration_reads_the_way_a_person_says_it(self):
        for seconds, words in (
            (0, "0s"),
            (59, "59s"),
            (60, "1m"),
            (3599, "59m"),
            (3600, "1h"),
            (3660, "1h 1m"),
            (7200, "2h"),
            (-5, "0s"),
        ):
            self.assertEqual(alonetime.duration_words(seconds), words, seconds)

    def test_the_status_card_says_which_half_is_in_force(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7, started_at=1000.0)
        rows = dict(alonetime.status_lines(lock, alonetime.Settings(), now=1000.0))
        self.assertEqual(rows["Guests"], "none")
        self.assertNotIn("Move Members", rows["In force"])
        missing = dict(
            alonetime.status_lines(lock, alonetime.Settings(), now=1000.0, guard_ready=False)
        )
        self.assertIn("Move Members", missing["In force"])
        off = dict(
            alonetime.status_lines(lock, alonetime.Settings(guard=False), now=1000.0)
        )
        self.assertIn("off", off["In force"])

    def test_the_capacity_row_says_what_the_cap_actually_is(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7, started_at=1000.0)
        rows = dict(alonetime.status_lines(lock, alonetime.Settings()))
        self.assertIn("nobody else", rows["Capacity"])

    def test_a_quiet_hours_room_says_whose_it_is(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=0)
        rows = dict(alonetime.status_lines(lock, alonetime.Settings()))
        self.assertIn("quiet hours", rows["Holder"])

    def test_the_lock_report_says_the_capacity_and_which_half_will_not_run(self):
        report = alonetime.Report(
            action="lock", ok=True, channel="Lobby", capacity=1, guard_missing=True,
            detail="⚠️ I do not have **Move Members** here.",
        )
        said = report.sentence()
        self.assertIn("Lobby", said)
        self.assertIn("**1**", said)
        self.assertIn("administrator", said.lower())
        self.assertIn("Move Members", said)
        self.assertTrue(report.to_dict()["guard_missing"])


class TheServersSettings(unittest.TestCase):
    def _member(self, *, admin=False, manage=False, roles=()):
        perms = _Perms(administrator=admin, manage_channels=manage)
        return _Member(7, "someone", roles=roles, perms=perms)

    def test_the_default_lets_everybody_lock_their_own_room(self):
        self.assertTrue(alonetime.Settings().may_lock(self._member()))

    def test_administrators_only_means_exactly_that(self):
        settings = alonetime.Settings(who=alonetime.WHO_ADMINS)
        self.assertTrue(settings.may_lock(self._member(admin=True)))
        self.assertTrue(settings.may_lock(self._member(manage=True)))
        self.assertFalse(settings.may_lock(self._member()))

    def test_a_named_role_is_matched_by_name_or_by_id(self):
        role = _Role(42, "Quiet Hours")
        settings = alonetime.Settings(who=alonetime.WHO_ROLE, role_name="quiet hours")
        self.assertTrue(settings.may_lock(self._member(roles=[role])))
        by_id = alonetime.Settings(who=alonetime.WHO_ROLE, role_name="42")
        self.assertTrue(by_id.may_lock(self._member(roles=[role])))

    def test_a_role_setting_never_locks_an_administrator_out(self):
        settings = alonetime.Settings(who=alonetime.WHO_ROLE, role_name="nobody")
        self.assertTrue(settings.may_lock(self._member(admin=True)))

    def test_an_empty_role_name_matches_nobody(self):
        settings = alonetime.Settings(who=alonetime.WHO_ROLE, role_name="  ")
        self.assertFalse(settings.may_lock(self._member(roles=[_Role(42, "  ")])))

    def test_a_stored_value_that_is_not_a_choice_reads_as_the_default(self):
        settings = alonetime.settings_from({"who": "sometimes", "cooldown_seconds": "abc"})
        self.assertEqual(settings.who, alonetime.WHO_EVERYONE)
        self.assertEqual(settings.cooldown_seconds, 60)

    def test_a_stored_number_out_of_range_reads_as_the_default_not_clamped(self):
        """A corrupted row may not become a *larger* allowance than the default."""
        settings = alonetime.settings_from({"max_hours": "99999"})
        self.assertEqual(settings.max_hours, features.get_setting("alonetime", "max_hours").default)

    def test_off_says_the_systems_name_and_where_it_is_switched_on(self):
        said = alonetime.off_sentence()
        self.assertIn("Alone Time", said)
        self.assertIn("turned off", said)
        self.assertIn("/dashboard", said)


class ThePublishedReading(unittest.TestCase):
    """Ids and counts only: the reading is published, and names are not."""

    def tearDown(self):
        alonetime.invalidate()

    def test_the_reading_carries_no_name_of_anybody(self):
        alonetime.CACHE[4242] = {
            7: alonetime.Lock(
                guild_id=4242, channel_id=7, holder_id=9, holder_name="rek_ter",
                guests=(11, 12), turned_away=2, started_at=1000.0,
            )
        }
        (row,) = alonetime.locked_reading(4242, now=1000.0)
        self.assertNotIn("name", row)
        self.assertEqual(row["guests"], 2)
        self.assertEqual(row["holder_id"], "9")
        self.assertEqual(row["channel_id"], "7")
        self.assertEqual(row["turned_away"], 2)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class _EngineCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_temp_database("alonetime.db")
        await db.setup()
        alonetime.invalidate()

    async def asyncTearDown(self):
        alonetime.invalidate()
        self.release_temp_database()

    def _guild(self, *, move_members=True, manage=True):
        # ``manage`` goes to *every* room: a server whose bot cannot manage one
        # channel and can manage the next is the case a quiet-hours window has to
        # report honestly, so the stand-in can be built that way too.
        guild = _Guild(move_members=move_members)
        guild.room = guild.add_voice_channel("Lobby", manage=manage)
        guild.elsewhere = guild.add_voice_channel("Music", manage=manage)
        return guild


class TheLock(_EngineCase):
    async def test_the_undo_is_written_before_the_first_change(self):
        guild = self._guild()
        room = guild.room
        real = db.save_alone_time

        async def watched(*args, **kwargs):
            room.journal.append(("ledger",))
            return await real(*args, **kwargs)

        with mock.patch.object(alonetime.db, "save_alone_time", new=watched):
            report = await alonetime.lock_channel(
                guild, room, _Member(7, "rek_ter", guild=guild)
            )
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.journal[0], ("ledger",), "the ledger must come first")
        self.assertEqual(room.journal[1][0], "set_permissions")

    async def test_the_cap_is_the_layer_an_administrator_cannot_walk_past(self):
        guild = self._guild()
        room = guild.room
        report = await alonetime.lock_channel(guild, room, _Member(7, "rek_ter", guild=guild))
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.user_limit, 1, "the cap is what holds the room")
        self.assertIs(room.overwrites[guild.default_role].connect, False)
        self.assertIn("🔒", room.name)

    async def test_a_room_it_cannot_manage_is_refused_before_anything_is_changed(self):
        guild = self._guild(manage=False)
        report = await alonetime.lock_channel(guild, guild.room, _Member(7, guild=guild))
        self.assertFalse(report.ok)
        self.assertIn("Manage Channels", report.detail)
        self.assertEqual(guild.room.journal, [])
        self.assertEqual(await db.alone_time_rows(guild.id), [])

    async def test_a_ledger_that_cannot_be_written_means_nothing_happens(self):
        guild = self._guild()
        with mock.patch.object(
            alonetime.db, "save_alone_time",
            new=mock.AsyncMock(side_effect=RuntimeError("disk full")),
        ):
            report = await alonetime.lock_channel(guild, guild.room, _Member(7, guild=guild))
        self.assertFalse(report.ok)
        self.assertEqual(guild.room.journal, [], "a change with no undo is not worth making")
        self.assertIn("write down", report.detail)

    async def test_a_refused_write_leaves_no_row_claiming_a_locked_room(self):
        guild = self._guild()
        room = guild.room

        async def refuse(*_args, **_kwargs):
            raise RuntimeError("nope")

        room.set_permissions = refuse
        report = await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        self.assertFalse(report.ok)
        self.assertIn("open", report.detail)
        self.assertIsNone(await db.alone_time_row(guild.id, room.id))
        self.assertIsNone(alonetime.lock_for(guild.id, room.id))

    async def test_a_lock_without_move_members_says_which_half_will_not_run(self):
        guild = self._guild(move_members=False)
        report = await alonetime.lock_channel(guild, guild.room, _Member(7, guild=guild))
        self.assertTrue(report.ok)
        self.assertTrue(report.guard_missing)
        self.assertIn("Move Members", report.sentence())

    async def test_the_holder_is_recorded_by_name_and_not_by_mention(self):
        guild = self._guild()
        holder = _Member(7, "rek_ter", guild=guild)
        await alonetime.lock_channel(guild, guild.room, holder)
        row = await db.alone_time_row(guild.id, guild.room.id)
        self.assertEqual(row["holder_id"], 7)
        self.assertEqual(row["holder_name"], "rek_ter")
        self.assertEqual(row["previous_name"], "Lobby")
        self.assertEqual(row["previous_limit"], 0)

    async def test_a_quiet_hours_lock_is_held_by_nobody(self):
        guild = self._guild()
        report = await alonetime.lock_channel(guild, guild.room, None)
        self.assertTrue(report.ok)
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertTrue(lock.server_wide)


class TheRelease(_EngineCase):
    async def test_a_room_with_no_overwrite_is_restored_by_deleting_not_allowing(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        report = await alonetime.release_channel(guild, lock)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.overwrites, {}, "an allow here is a permission nobody granted")
        self.assertEqual(room.user_limit, 0)
        self.assertEqual(room.name, "Lobby")
        self.assertIsNone(await db.alone_time_row(guild.id, room.id))

    async def test_an_overwrite_the_room_already_had_is_put_back_exactly(self):
        guild = self._guild()
        room = guild.room
        room.overwrites[guild.default_role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(8), discord.Permissions(16)
        )
        room.user_limit = 5
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        self.assertEqual(lock.previous, "8:16", "the ledger records the room's own numbers")
        await alonetime.release_channel(guild, lock)
        restored = room.overwrites[guild.default_role]
        self.assertIsInstance(restored, discord.PermissionOverwrite, "text, not a live object")
        allowed, denied = restored.pair()
        self.assertEqual(allowed.value, 8)
        self.assertEqual(denied.value, 16)
        self.assertEqual(room.user_limit, 5)

    async def test_a_rename_made_after_the_lock_is_left_alone(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        room.name = "Renamed by a person"
        lock = alonetime.lock_for(guild.id, room.id)
        await alonetime.release_channel(guild, lock)
        self.assertEqual(room.name, "Renamed by a person")

    async def test_a_room_that_no_longer_exists_is_released_and_forgotten(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        guild.gone.add(room.id)
        lock = alonetime.lock_for(guild.id, room.id)
        report = await alonetime.release_channel(guild, lock)
        self.assertTrue(report.ok)
        self.assertIn("no longer exists", report.detail)
        self.assertIsNone(await db.alone_time_row(guild.id, room.id))

    async def test_a_refused_restore_keeps_the_row_and_says_what_is_missing(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)

        async def refuse(*_args, **_kwargs):
            raise RuntimeError("nope")

        room.set_permissions = refuse
        report = await alonetime.release_channel(guild, lock)
        self.assertFalse(report.ok)
        self.assertIn("Manage Channels", report.detail)
        self.assertIsNotNone(await db.alone_time_row(guild.id, room.id), "the ledger must survive")

    async def test_releasing_from_a_place_that_has_only_an_id_is_the_same_path(self):
        guild = self._guild()
        await alonetime.lock_channel(guild, guild.room, _Member(7, guild=guild))
        report = await alonetime.release_by_channel_id(guild, guild.room.id)
        self.assertTrue(report.ok, report.detail)
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_releasing_a_room_that_is_not_locked_is_not_an_error(self):
        guild = self._guild()
        report = await alonetime.release_by_channel_id(guild, 123456)
        self.assertTrue(report.ok)
        self.assertIn("not locked", report.detail)


class TheGuestList(_EngineCase):
    async def test_the_cap_rises_to_fit_every_guest(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        await alonetime.write_guests(guild.id, room.id, lock, guests=(8, 9))
        updated = alonetime.lock_for(guild.id, room.id)
        self.assertEqual(updated.guests, (8, 9))
        self.assertTrue(updated.admits(8))
        self.assertFalse(updated.admits(10))
        self.assertEqual(alonetime.capacity_for(guild, updated.guests), 3)

    async def test_the_guest_list_survives_a_reload_from_the_ledger(self):
        guild = self._guild()
        await alonetime.lock_channel(guild, guild.room, _Member(7, guild=guild))
        lock = alonetime.lock_for(guild.id, guild.room.id)
        await alonetime.write_guests(guild.id, guild.room.id, lock, guests=(8,), guest_roles=(42,))
        alonetime.invalidate(guild.id)
        from_disk = alonetime.lock_for(guild.id, guild.room.id)
        self.assertEqual(from_disk.guests, (8,))
        self.assertEqual(from_disk.guest_roles, (42,))


class TheQuietHours(_EngineCase):
    async def test_every_room_but_the_afk_one_and_the_stages_is_locked(self):
        guild = self._guild()
        stage = guild.add_voice_channel("Town Hall", type_="stage_voice")
        guild.afk_channel = guild.add_voice_channel("AFK")
        report = await alonetime.lock_all_voice(guild, minutes=30)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.changed, 2)
        self.assertIsNone(alonetime.lock_for(guild.id, stage.id))
        self.assertIsNone(alonetime.lock_for(guild.id, guild.afk_channel.id))
        self.assertTrue(alonetime.lock_for(guild.id, guild.room.id).server_wide)

    async def test_a_room_somebody_holds_is_skipped_rather_than_relocked(self):
        guild = self._guild()
        await alonetime.lock_channel(guild, guild.room, _Member(7, "rek_ter", guild=guild))
        report = await alonetime.lock_all_voice(guild, minutes=30)
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.skipped, 1)
        held = alonetime.lock_for(guild.id, guild.room.id)
        self.assertEqual(held.holder_id, 7, "the member's own room is not the window's")

    async def test_the_window_has_a_clock_and_giving_it_back_clears_it(self):
        guild = self._guild()
        await alonetime.lock_all_voice(guild, minutes=30)
        self.assertGreater(await alonetime.server_window_end(guild.id), time.time())
        report = await alonetime.release_server_window(guild, reason="ended early")
        self.assertTrue(report.ok)
        self.assertEqual(report.changed, 2)
        self.assertEqual(await alonetime.server_window_end(guild.id), 0.0)
        self.assertEqual(guild.room.user_limit, 0)
        self.assertEqual(guild.elsewhere.name, "Music")

    async def test_the_windows_end_never_gives_back_somebodys_own_room(self):
        guild = self._guild()
        await alonetime.lock_channel(guild, guild.room, _Member(7, "rek_ter", guild=guild))
        await alonetime.lock_all_voice(guild, minutes=30)
        report = await alonetime.release_server_window(guild)
        self.assertEqual(report.changed, 1)
        held = alonetime.lock_for(guild.id, guild.room.id)
        self.assertIsNotNone(held, "the member's room is not the window's to give back")
        self.assertEqual(held.holder_id, 7)

    async def test_a_window_that_locks_nothing_is_a_refusal_and_says_why(self):
        guild = self._guild(manage=False)
        report = await alonetime.lock_all_voice(guild, minutes=30)
        self.assertFalse(report.ok)
        self.assertIn("Manage Channels", report.detail)
        self.assertEqual(await alonetime.server_window_end(guild.id), 0.0)


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #
class _CogCase(TempDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = None

    def tearDown(self):
        if self.cog is not None:
            self.cog.reconcile.cancel()

    async def asyncSetUp(self):
        self.use_temp_database("alonetime.db")
        await db.setup()
        alonetime.invalidate()
        self._clear_session_memory()

    async def asyncTearDown(self):
        alonetime.invalidate()
        self._clear_session_memory()
        self.release_temp_database()

    @staticmethod
    def _clear_session_memory():
        """The two stores are process-wide by design, so a test owns them.

        ``utils/alonetime`` keeps reasons and requests in memory precisely
        because they must not be rows, which means one test's leftover request
        would be the next test's state — the isolation a database gives for free
        is this helper's job here.
        """
        alonetime.forget_reason()
        alonetime.forget_requests()
        alonetime.forget_ask_cards()
        alonetime.LAST_REQUEST.clear()

    def _guild(self, *, move_members=True, module_on=True):
        guild = _Guild(move_members=move_members)
        guild.room = guild.add_voice_channel("Lobby")
        guild.elsewhere = guild.add_voice_channel("Music")
        self.bot = _Bot([guild], module_on=module_on)
        return guild

    async def _cog(self, guild):
        from cogs.alonetime import AloneTime

        self.cog = AloneTime(self.bot)
        return self.cog

    async def _setting(self, guild, setting_id, value):
        await db.set_setting(guild.id, features.setting_key("alonetime", setting_id), value)

    async def _locked(self, guild, *, holder=None, guests=(), keep=None, note=""):
        holder = holder or _Member(7, "rek_ter", guild=guild)
        # In the guild's member cache, the way a real holder is: the request card
        # is DMed, and that lookup goes through ``Guild.get_member`` first.
        guild.members.append(holder)
        report = await alonetime.lock_channel(
            guild, guild.room, holder, guests=guests, keep=keep, note=note, actor="tester"
        )
        self.assertTrue(report.ok, report.detail)
        return holder


class TheSweep(_CogCase):
    async def test_a_stranger_is_put_back_where_they_came_from(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        self.assertEqual(stranger.moves, [guild.elsewhere])

    async def test_the_notice_lands_where_the_person_ended_up(self):
        """They cannot read the locked room's chat any more — the sentence follows them."""
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        said = _card_text(guild.elsewhere)
        self.assertIn("Nosey", said)
        self.assertIn("rek_ter", said)
        self.assertEqual(_card_text(guild.room), "", "not in the room they were turned out of")

    async def test_the_holder_and_the_guests_are_left_alone(self):
        guild = self._guild()
        await self._cog(guild)
        holder = await self._locked(guild, guests=(8,))
        for member in (holder, _Member(8, "friend", guild=guild)):
            await self.cog._voice_event(member, _state(guild.elsewhere), _state(guild.room))
            self.assertEqual(member.moves, [], member.display_name)

    async def test_a_guest_role_is_admitted_too(self):
        guild = self._guild()
        await self._cog(guild)
        role = _Role(42, "staff", members=[_Member(9, "staffer", guild=guild)])
        await alonetime.lock_channel(
            guild, guild.room, _Member(7, "rek_ter", guild=guild), guest_roles=(role,)
        )
        staffer = _Member(9, "staffer", guild=guild, roles=[role])
        await self.cog._voice_event(staffer, _state(None), _state(guild.room))
        self.assertEqual(staffer.moves, [])

    async def test_a_bot_arriving_is_not_this_features_business(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        robot = _Member(555, "MusicBot", bot=True, guild=guild)
        await self.cog.on_voice_state_update(robot, _state(None), _state(guild.room))
        self.assertEqual(robot.moves, [])

    async def test_a_member_with_nowhere_to_go_is_dropped_out_of_voice(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        wanderer = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(wanderer, _state(None), _state(guild.room))
        self.assertEqual(wanderer.moves, [None])

    async def test_a_server_that_asked_for_the_cap_alone_gets_no_sweep(self):
        guild = self._guild()
        await self._cog(guild)
        await self._setting(guild, "guard", "off")
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        self.assertEqual(stranger.moves, [], "the server asked for the cap and nothing else")

    async def test_discord_refusing_the_move_is_reported_and_never_pretended_away(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)

        async def refuse(_target, reason=""):
            raise RuntimeError("above me in the role list")

        stranger.move_to = refuse
        # The listener must outlive one member's event, and the holder must find out.
        await self.cog.on_voice_state_update(
            stranger, _state(guild.elsewhere), _state(guild.room)
        )
        said = _card_text(guild.room)
        self.assertIn("could not move", said)
        self.assertIn("Nosey", said)

    async def test_the_holder_leaving_gives_the_room_back(self):
        guild = self._guild()
        await self._cog(guild)
        holder = await self._locked(guild)
        await self.cog._voice_event(holder, _state(guild.room), _state(None))
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))
        self.assertEqual(guild.room.user_limit, 0)
        self.assertEqual(guild.room.name, "Lobby")
        self.assertIn("is over", _card_text(guild.room))

    async def test_a_room_asked_to_stay_locked_is_not_given_back_on_leaving(self):
        guild = self._guild()
        await self._cog(guild)
        holder = await self._locked(guild, keep=True)
        await self.cog._voice_event(holder, _state(guild.room), _state(None))
        self.assertIsNotNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_a_quiet_hours_room_is_not_given_back_by_a_holder_that_was_never_there(self):
        guild = self._guild()
        await self._cog(guild)
        await alonetime.lock_all_voice(guild, minutes=30)
        somebody = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(somebody, _state(guild.room), _state(None))
        self.assertIsNotNone(await db.alone_time_row(guild.id, guild.room.id))


class TheReconcileLoop(_CogCase):
    async def test_a_room_whose_holder_left_is_released_on_the_next_pass(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)  # the holder is not in ``room.members``
        await self.cog._reconcile_once()
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_a_holder_still_sitting_in_the_room_keeps_it(self):
        guild = self._guild()
        await self._cog(guild)
        holder = await self._locked(guild)
        guild.room.members = [holder]
        await self.cog._reconcile_once()
        self.assertIsNotNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_a_room_that_no_longer_exists_is_forgotten(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        guild.gone.add(guild.room.id)
        await self.cog._reconcile_once()
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_switching_the_system_off_cannot_leave_a_room_locked(self):
        """A system that is off must not be holding somebody's room."""
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        self.bot.is_module_enabled = lambda _guild_id, _module: False
        await self.cog._reconcile_once()
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))
        self.assertEqual(guild.room.user_limit, 0)

    async def test_a_window_whose_clock_ran_out_gives_every_room_back(self):
        guild = self._guild()
        await self._cog(guild)
        await alonetime.lock_all_voice(guild, minutes=30)
        await db.set_setting(
            guild.id, alonetime.SERVER_UNTIL_KEY, time.time() - 1
        )
        await self.cog._reconcile_once()
        self.assertEqual(await alonetime.server_window_end(guild.id), 0.0)
        self.assertEqual(guild.room.user_limit, 0)
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))

    async def test_a_loop_that_raises_does_not_take_the_feature_down(self):
        guild = self._guild()
        await self._cog(guild)
        await self._locked(guild)
        with mock.patch.object(
            alonetime, "release_channel",
            new=mock.AsyncMock(side_effect=RuntimeError("nope")),
        ):
            # The loop's own body, which is where the guard lives: a pass that
            # raises must not stop the next one.
            await self.cog.reconcile()
        self.assertIsNotNone(await db.alone_time_row(guild.id, guild.room.id))


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #
class TheCommands(_CogCase):
    async def test_a_switched_off_system_answers_at_the_door(self):
        guild = self._guild(module_on=False)
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        interaction = _Interaction(guild, member)
        self.assertFalse(await cog.interaction_check(interaction))
        said = _said(interaction)
        self.assertIn("turned off", said)
        self.assertIn("Alone Time", said)

    async def test_a_member_not_in_a_room_is_told_where_to_stand(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        interaction = _Interaction(guild, member)
        await AloneTime.alone_start.callback(cog, interaction, None, None)
        self.assertIn("Join the voice channel", _said(interaction))
        self.assertEqual(await db.alone_time_rows(guild.id), [])

    async def test_locking_a_room_caps_it_and_says_the_capacity_is_what_holds_it(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        member.voice.channel = guild.room
        interaction = _Interaction(guild, member)
        await AloneTime.alone_start.callback(cog, interaction, "studying", None)
        row = await db.alone_time_row(guild.id, guild.room.id)
        self.assertIsNotNone(row)
        self.assertEqual(row["note"], "", "the reason is memory, never a row")
        self.assertEqual(guild.room.user_limit, 1)
        said = _said(interaction)
        self.assertIn("locked to its holder", said)
        self.assertIn("capacity", said.lower())
        self.assertIn("administrator", said.lower())

    async def test_a_room_somebody_else_holds_is_refused_by_name(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        other = _Member(8, "Other", guild=guild)
        other.voice.channel = guild.room
        interaction = _Interaction(guild, other)
        await AloneTime.alone_start.callback(cog, interaction, None, None)
        said = _said(interaction)
        self.assertIn("held by", said)
        self.assertIn("rek_ter", said)

    async def test_the_cooldown_holds_the_same_member_back(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        member.voice.channel = guild.room
        await AloneTime.alone_start.callback(cog, _Interaction(guild, member), None, None)
        member.voice.channel = guild.elsewhere
        interaction = _Interaction(guild, member)
        await AloneTime.alone_start.callback(cog, interaction, None, None)
        self.assertIn("before you can lock another room", _said(interaction))
        self.assertIsNone(await db.alone_time_row(guild.id, guild.elsewhere.id))

    async def test_stop_puts_the_room_back_and_says_so(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        interaction = _Interaction(guild, holder)
        await AloneTime.alone_stop.callback(cog, interaction)
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))
        self.assertEqual(guild.room.user_limit, 0)
        self.assertIn("open again", _said(interaction))

    async def test_stop_refuses_somebody_who_holds_nothing(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        interaction = _Interaction(guild, member)
        await AloneTime.alone_stop.callback(cog, interaction)
        self.assertIn("not holding a room", _said(interaction))

    async def test_release_is_for_the_people_the_room_is_not_held_by(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        admin = _Member(9, "Admin", guild=guild, perms=_Perms(manage_channels=True))
        interaction = _Interaction(guild, admin)
        await AloneTime.alone_release.callback(cog, interaction, guild.room)
        self.assertIsNone(await db.alone_time_row(guild.id, guild.room.id))
        self.assertIn("open again", _said(interaction))

    async def test_release_is_gated_by_manage_channels(self):
        from cogs.alonetime import AloneTime

        checks = " ".join(str(check) for check in AloneTime.alone_release.checks)
        self.assertIn("has_permissions", checks)

    async def test_quiet_hours_locks_every_room_and_says_when_it_lifts(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        admin = _Member(9, "Admin", guild=guild, perms=_Perms(administrator=True))
        interaction = _Interaction(guild, admin)
        await AloneTime.alone_server.callback(cog, interaction, None, False)
        said = _said(interaction)
        self.assertIn("2", said)
        self.assertIn("60", said, "the server's own default window")
        self.assertEqual(guild.room.user_limit, 1)
        self.assertTrue(alonetime.lock_for(guild.id, guild.elsewhere.id).server_wide)

    async def test_giving_the_window_back_early_restores_every_room(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        admin = _Member(9, "Admin", guild=guild, perms=_Perms(administrator=True))
        await AloneTime.alone_server.callback(cog, _Interaction(guild, admin), None, False)
        interaction = _Interaction(guild, admin)
        await AloneTime.alone_server.callback(cog, interaction, None, True)
        self.assertEqual(await alonetime.server_window_end(guild.id), 0.0)
        self.assertEqual(guild.room.user_limit, 0)

    async def test_quiet_hours_is_gated_by_administrator(self):
        from cogs.alonetime import AloneTime

        checks = " ".join(str(check) for check in AloneTime.alone_server.checks)
        self.assertIn("has_permissions", checks)

    async def test_the_guest_list_raises_the_cap_so_the_guest_can_get_in(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        holder.voice.channel = guild.room
        guest = _Member(8, "friend", guild=guild)
        interaction = _Interaction(guild, holder)
        await AloneTime.alone_guests.callback(cog, interaction, guest, None, False)
        self.assertEqual(guild.room.user_limit, 2, "a guest nobody can let past the cap")
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertTrue(lock.admits(8))
        self.assertIn("whitelist", _said(interaction).lower())

    async def test_taking_a_guest_off_puts_the_cap_back(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild, guests=(8,))
        holder.voice.channel = guild.room
        guest = _Member(8, "friend", guild=guild)
        interaction = _Interaction(guild, holder)
        await AloneTime.alone_guests.callback(cog, interaction, guest, None, True)
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertEqual(lock.guests, ())
        self.assertEqual(guild.room.user_limit, 1)
        self.assertFalse(lock.admits(8))

    async def test_the_guest_list_is_the_holders_to_change(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        stranger.voice.channel = guild.room
        # Even with Manage Channels: the whitelist is the holder's, and the lever
        # a moderator has is `/alone release`, not somebody else's guest list.
        stranger.guild_permissions = _Perms(manage_channels=True)
        interaction = _Interaction(guild, stranger)
        await AloneTime.alone_guests.callback(cog, interaction, _Member(9, "x", guild=guild), None, False)
        said = _said(interaction)
        self.assertIn("belongs to whoever holds the room", said)
        self.assertIn("/alone release", said)
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertEqual(lock.guests, (), "nothing was written by a stranger")

    async def test_a_switched_off_system_does_not_move_anybody(self):
        """Off means off: the only part of this feature still running is nothing."""
        guild = self._guild(module_on=False)
        await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self.cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        self.assertEqual(stranger.moves, [])


# --------------------------------------------------------------------------- #
# The wiring: registry, permission table, help and the reading
# --------------------------------------------------------------------------- #
class TheRegistry(unittest.TestCase):
    def test_every_subcommand_declares_who_may_run_it(self):
        for path, audience in (
            ("alone start", permissions.EVERYONE),
            ("alone stop", permissions.EVERYONE),
            ("alone status", permissions.EVERYONE),
            ("alone guests", permissions.EVERYONE),
            ("alone ask", permissions.EVERYONE),
            ("alone list", permissions.CHANNELS),
            ("alone release", permissions.CHANNELS),
            ("alone server", permissions.ADMIN),
        ):
            self.assertEqual(permissions.COMMAND_ACCESS.get(path), audience, path)

    def test_the_registry_row_names_a_cog_that_ships(self):
        import os

        feature = features.get("alonetime")
        self.assertIsNotNone(feature)
        self.assertEqual(feature.gate_commands, ("alone",))
        self.assertTrue(os.path.exists(f"{feature.cog.replace('.', '/')}.py"), feature.cog)

    def test_the_help_menu_has_a_category_for_it(self):
        from cogs import help as help_cog

        self.assertIn("alonetime", help_cog.HELP_MODULES)

    def test_it_ships_off_and_one_per_guild_switch_turns_it_on(self):
        """The beta's whole shape: off everywhere, one server asks for it.

        And the gate is the picker, not a refusal: a server that has not opted in
        never lists `/alone` at all, which is the difference between a feature
        being switched off and a command that answers "no".
        """
        from utils import command_scope

        feature = features.get("alonetime")
        self.assertFalse(feature.enabled_by_default)
        self.assertIn("alonetime", features.DEFAULT_DISABLED_MODULES)
        self.assertIn("alonetime", db.DEFAULT_DISABLED_MODULES)
        self.assertFalse(features.default_enabled("alonetime"))
        self.assertFalse(command_scope.is_available("alone", {}))
        self.assertTrue(command_scope.is_available("alone", {"alonetime": True}))

    def test_the_room_is_held_by_the_capacity_and_the_sweep_is_named_as_the_backstop(self):
        feature = features.get("alonetime")
        description = feature.description.lower()
        self.assertIn("not an administrator", description)
        self.assertIn("capacity", description)


# --------------------------------------------------------------------------- #
# What this pass fixed, and what the beta added
# --------------------------------------------------------------------------- #
class TheRenameIsNotTheLock(_EngineCase):
    """Discord rate-limits renames; a refused one must not cost the lock.

    This is the defect that made a locked room look like nothing had happened:
    the rename shared the permission write's ``try``, so a 429 un-wrote the ledger
    row and left a room locked with nobody able to release it.
    """

    async def test_a_refused_rename_keeps_the_lock_and_its_row(self):
        guild = self._guild()
        room = guild.room
        real = room.edit

        async def refuse_the_name(*, name=None, user_limit=None, reason=""):
            if name is not None:
                raise RuntimeError("the name edit was refused")
            return await real(name=name, user_limit=user_limit, reason=reason)

        room.edit = refuse_the_name
        report = await alonetime.lock_channel(guild, room, _Member(7, "rek_ter", guild=guild))
        self.assertTrue(report.ok, "the lock applied, and must be reported as applied")
        self.assertEqual(room.user_limit, 1, "the cap is the layer that matters")
        self.assertIsNotNone(
            await db.alone_time_row(guild.id, room.id),
            "the row is the undo — without it the room can never be opened again",
        )
        self.assertIn("RuntimeError", report.name_warning)
        self.assertIn("RuntimeError", report.sentence())

    async def test_a_refused_name_restore_still_gives_the_row_back(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, "rek_ter", guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        real = room.edit

        async def refuse_the_name(*, name=None, user_limit=None, reason=""):
            if name is not None:
                raise RuntimeError("the name edit was refused")
            return await real(name=name, user_limit=user_limit, reason=reason)

        room.edit = refuse_the_name
        report = await alonetime.release_channel(guild, lock)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.user_limit, 0, "the room is open again")
        self.assertIsNone(await db.alone_time_row(guild.id, room.id), "and the row is gone")
        self.assertEqual(room.name, "🔒 Lobby", "the name is the only thing left behind")
        self.assertTrue(report.name_warning)

    async def test_a_custom_name_is_what_goes_back_on_release(self):
        guild = self._guild()
        room = guild.room
        report = await alonetime.lock_channel(
            guild, room, _Member(7, "rek_ter", guild=guild), name="Study"
        )
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.name, "🔒 Study")
        row = await db.alone_time_row(guild.id, room.id)
        self.assertEqual(row["previous_name"], "Lobby")
        self.assertEqual(row["locked_as"], "🔒 Study", "what we set is written down")
        lock = alonetime.lock_for(guild.id, room.id)
        self.assertEqual(lock.locked_as, "🔒 Study")
        self.assertTrue((await alonetime.release_channel(guild, lock)).ok)
        self.assertEqual(room.name, "Lobby")

    async def test_a_rename_by_somebody_else_is_not_undone(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, "rek_ter", guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        room.name = "Sound Booth"  # somebody renamed it after the lock
        report = await alonetime.release_channel(guild, lock)
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.name, "Sound Booth", "their decision is newer than ours")

    async def test_a_holder_rename_is_the_name_that_comes_back(self):
        guild = self._guild()
        room = guild.room
        await alonetime.lock_channel(guild, room, _Member(7, "rek_ter", guild=guild))
        lock = alonetime.lock_for(guild.id, room.id)
        report = await alonetime.rename_locked(guild, room, lock, "Study Room", actor="rek_ter")
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(room.name, "🔒 Study Room")
        fresh = alonetime.lock_for(guild.id, room.id)
        self.assertEqual(fresh.locked_as, "🔒 Study Room")
        self.assertTrue((await alonetime.release_channel(guild, fresh)).ok)
        self.assertEqual(room.name, "Lobby")


class TheRoomAsItAlreadyIs(_EngineCase):
    """The listener answers arrivals; a fresh lock owes the room one look."""

    def _room(self):
        return _Channel(None, 2)

    def test_strays_are_everybody_the_room_does_not_admit(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7)
        room = self._room()
        room.members = [
            _Member(7, "holder"),
            _Member(8, "Nosey"),
            _Member(9, "Admin", perms=_Perms(administrator=True)),
            _Member(999, "Meow", bot=True),
        ]
        self.assertEqual([member.id for member in alonetime.strays_in(room, lock)], [8, 9])

    def test_a_guest_and_a_guest_role_are_not_strays(self):
        lock = alonetime.Lock(
            guild_id=1, channel_id=2, holder_id=7, guests=(8,), guest_roles=(42,)
        )
        room = self._room()
        room.members = [
            _Member(7, "holder"),
            _Member(8, "friend"),
            _Member(9, "Fam", roles=[_Role(42)]),
        ]
        self.assertEqual(alonetime.strays_in(room, lock), [])

    def test_the_bots_own_connection_is_never_a_stray(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7)
        room = self._room()
        room.members = [_Member(999, "Meow", bot=True)]
        self.assertEqual(alonetime.strays_in(room, lock, bot_id=999), [])


class TheBetaSession(_CogCase):
    """The holder's controls, on top of everything the commands already did."""

    async def test_the_controls_are_registered_once_and_answer_by_custom_id(self):
        from cogs import alonetime as cog_module

        guild = self._guild()
        await self._cog(guild)
        self.assertTrue(self.bot.views, "the controls must outlive the process")
        registered = {
            item.custom_id for view in self.bot.views for item in view.children
        }
        # The holder's three, the one on a room's public notice, and the two on
        # the request card — one registration each, fixed ids, so a card posted
        # before the last redeploy still answers.
        self.assertEqual(
            registered,
            {
                cog_module.WHITELIST_ID, cog_module.RENAME_ID, cog_module.RELEASE_ID,
                cog_module.ASK_ID, cog_module.ALLOW_ID, cog_module.DENY_ID,
            },
        )
        holder_ids = {item.custom_id for item in self.bot.views[0].children}
        self.assertEqual(
            holder_ids, {cog_module.WHITELIST_ID, cog_module.RENAME_ID, cog_module.RELEASE_ID}
        )

    async def test_the_holder_can_whitelist_from_the_card(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        holder.voice.channel = guild.room
        interaction = _Interaction(guild, holder)
        await cog.whitelist_people(interaction, [8])
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertTrue(lock.admits(8))
        self.assertEqual(guild.room.user_limit, 2, "the cap has to fit the whitelist")
        card = _card_of(interaction)
        self.assertIn("Just now", card)
        self.assertIn("whitelist", card.lower())

    async def test_a_member_who_holds_nothing_cannot_touch_the_list(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        mod = _Member(9, "Mod", guild=guild, perms=_Perms(manage_channels=True))
        interaction = _Interaction(guild, mod)
        await cog.whitelist_people(interaction, [8])
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertFalse(lock.admits(8), "a moderator is not a special case here")
        self.assertIn("/alone release", _said(interaction))

    async def test_the_holders_own_id_is_not_a_whitelist_entry(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        interaction = _Interaction(guild, holder)
        await cog.whitelist_people(interaction, [holder.id])
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertEqual(lock.guests, ())

    async def test_the_holder_renames_the_room_from_the_card(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        interaction = _Interaction(guild, holder)
        await cog.rename_held_room(interaction, "Study")
        self.assertEqual(guild.room.name, "🔒 Study")
        row = await db.alone_time_row(guild.id, guild.room.id)
        self.assertEqual(row["locked_as"], "🔒 Study")
        self.assertIn("Study", _card_of(interaction))

    async def test_a_lock_that_starts_under_two_people_clears_the_room(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = _Member(7, "rek_ter", guild=guild)
        nosey = _Member(8, "Nosey", guild=guild)
        holder.voice.channel = guild.room
        guild.room.members = [holder, nosey]
        interaction = _Interaction(guild, holder)
        await AloneTime.alone_start.callback(cog, interaction, None, None)
        self.assertEqual(nosey.moves, [None], "nowhere to put them back: out of voice")
        self.assertIn("locked to its holder", _card_of(interaction))

    async def test_a_turned_away_member_gets_the_access_denied_card(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        said = _card_text(guild.elsewhere)
        self.assertIn("This room is on Alone Time", said)
        self.assertIn("Ask to join", said, "the way back in is on the card they can read")
        self.assertIn("no private message for a voice join", said)


# --------------------------------------------------------------------------- #
# The reason nobody can read, the routing, and the request
# --------------------------------------------------------------------------- #
class ThePrivateReason(_CogCase):
    """The holder's reason is session memory: no row, no log, no other reader.

    The promise is *not even the bot's owner can read it*, and the only storage
    that makes that sentence true is no storage at all — so the laws here are all
    about absence, which is also the kind of claim that is easy to make and hard
    to keep. Every one of them reads a surface the text could have reached.
    """

    async def test_it_is_never_written_to_the_database(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        member.voice.channel = guild.room
        interaction = _Interaction(guild, member)
        await AloneTime.alone_start.callback(cog, interaction, "studying for finals", None)
        row = await db.alone_time_row(guild.id, guild.room.id)
        self.assertIsNotNone(row)
        self.assertEqual(row["note"], "", "the column is the one place it used to live")
        self.assertEqual(
            alonetime.reason_for(guild.id, guild.room.id), "studying for finals",
            "and the session memory is the only place it does live",
        )
        self.assertIn("studying for finals", _card_of(interaction))

    async def test_the_public_notice_never_carries_it(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        member = _Member(7, "rek_ter", guild=guild)
        member.voice.channel = guild.room
        await AloneTime.alone_start.callback(
            cog, _Interaction(guild, member), "the private part", None
        )
        self.assertIn("has this room to themselves", _card_text(house))
        self.assertNotIn("the private part", _card_text(house))
        self.assertEqual(_card_text(guild.room), "", "and not in the room either")

    async def test_status_shows_it_to_the_holder_and_to_nobody_else(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        alonetime.remember_reason(guild.id, guild.room.id, "not now please")
        holder.voice.channel = guild.room
        mine = _Interaction(guild, holder)
        await AloneTime.alone_status.callback(cog, mine)
        self.assertIn("not now please", _card_of(mine))
        other = _Interaction(guild, _Member(8, "Nosey", guild=guild))
        await AloneTime.alone_status.callback(cog, other)
        self.assertNotIn("not now please", _card_of(other))

    async def test_a_release_forgets_it(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        alonetime.remember_reason(guild.id, guild.room.id, "quiet, please")
        await AloneTime.alone_stop.callback(cog, _Interaction(guild, holder))
        self.assertEqual(alonetime.reason_for(guild.id, guild.room.id), "")

    async def test_a_departure_forgets_it_too(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild, note="gone in a moment")
        self.assertEqual(alonetime.reason_for(guild.id, guild.room.id), "gone in a moment")
        await self.cog._voice_event(holder, _state(guild.room), _state(None))
        self.assertEqual(alonetime.reason_for(guild.id, guild.room.id), "")

    async def test_the_moderation_log_gets_the_fact_and_not_the_text(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        member = _Member(7, "rek_ter", guild=guild)
        member.voice.channel = guild.room
        seen = []

        async def watch(_guild, _lock, action, detail, **_kwargs):
            seen.append((action, detail))

        with mock.patch.object(cog, "_case", new=watch):
            await AloneTime.alone_start.callback(
                cog, _Interaction(guild, member), "a secret nobody reads", None
            )
        self.assertTrue(seen, "a lock opens a case")
        joined = " ".join(f"{action} {detail}" for action, detail in seen)
        self.assertNotIn("a secret nobody reads", joined)
        self.assertIn("never writes down", joined)

    async def test_a_legacy_note_is_blanked_when_the_database_opens(self):
        """A lock taken before this change must not keep somebody's words."""
        guild = self._guild()
        await db.save_alone_time(guild.id, guild.room.id, holder_id=7, previous_name="Lobby")
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "UPDATE alone_time SET note = 'old words' "
                "WHERE guild_id = ? AND channel_id = ?",
                (guild.id, guild.room.id),
            )
            await conn.commit()
        await db.setup()
        row = await db.alone_time_row(guild.id, guild.room.id)
        self.assertEqual(row["note"], "", "the boot purge is the whole upgrade")


class TheNoticeRouting(_CogCase):
    """Notices go where the server keeps them, never into the locked room first."""

    async def _lock_with_notice(self, guild, cog):
        from cogs.alonetime import AloneTime

        member = _Member(7, "rek_ter", guild=guild)
        guild.members.append(member)
        member.voice.channel = guild.room
        await AloneTime.alone_start.callback(cog, _Interaction(guild, member), None, None)

    async def test_a_house_channel_is_where_the_notice_lands(self):
        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        await self._lock_with_notice(guild, cog)
        self.assertIn("has this room to themselves", _card_text(house))
        self.assertEqual(_card_text(guild.room), "", "not into the locked room")

    async def test_general_is_the_fallback_name(self):
        guild = self._guild()
        cog = await self._cog(guild)
        general = guild.add_text_channel("general")
        await self._lock_with_notice(guild, cog)
        self.assertIn("has this room to themselves", _card_text(general))

    async def test_the_servers_own_setting_wins_over_the_search(self):
        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        quiet = guild.add_text_channel("quiet-news")
        await self._setting(guild, "notice_channel", "quiet-news")
        await self._lock_with_notice(guild, cog)
        self.assertIn("has this room to themselves", _card_text(quiet))
        self.assertEqual(_card_text(house), "", "the setting is the server's answer")

    async def test_the_bound_commands_channel_beats_a_name_search(self):
        """``utils/bot_channels`` is the house-channel binding, and it is asked first."""
        guild = self._guild()
        cog = await self._cog(guild)
        bound = guild.add_text_channel("mod-stuff")
        general = guild.add_text_channel("general")
        await db.set_setting(guild.id, "bot_cmd_channel_id", bound.id)
        await self._lock_with_notice(guild, cog)
        self.assertIn("has this room to themselves", _card_text(bound))
        self.assertEqual(_card_text(general), "")

    async def test_the_locked_room_is_the_last_resort_and_not_the_first(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await self._lock_with_notice(guild, cog)
        self.assertIn(
            "has this room to themselves", _card_text(guild.room),
            "a server with nowhere else must still be told",
        )

    async def test_a_turned_away_member_is_noticed_in_the_house_channel(self):
        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        self.assertIn("This room is on Alone Time", _card_text(house))
        self.assertEqual(_card_text(guild.elsewhere), "", "and nowhere else")


class TheJoinRequest(_CogCase):
    """One request per member per five minutes, answered only by the holder."""

    async def _ask(self, cog, guild, member):
        interaction = _Interaction(guild, member)
        await cog.ask_to_join(interaction, room=guild.room)
        return interaction

    def test_the_cooldown_is_five_minutes_and_is_not_a_server_setting(self):
        self.assertEqual(alonetime.REQUEST_COOLDOWN_SECONDS, 300)
        alonetime.LAST_REQUEST.clear()
        alonetime.note_request(1, 2, now=1000.0)
        self.assertEqual(alonetime.request_left(1, 2, now=1000.0), 300)
        self.assertEqual(alonetime.request_left(1, 2, now=1200.0), 100)
        self.assertEqual(alonetime.request_left(1, 2, now=1300.0), 0)
        alonetime.LAST_REQUEST.clear()

    async def test_a_request_is_delivered_to_the_holders_dm(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        interaction = await self._ask(cog, guild, stranger)
        self.assertTrue(holder.dms, "the holder gets the request privately")
        card = str(holder.dms[-1]["view"].to_components())
        self.assertIn("Nosey", card)
        self.assertIn("asked to join", card)
        self.assertNotIn("Nosey", _card_text(guild.room), "and not in the room's chat")
        self.assertIn("Your request is with", _said(interaction))

    async def test_the_same_member_is_held_back_for_five_minutes(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self._ask(cog, guild, stranger)
        second = await self._ask(cog, guild, stranger)
        said = _said(second)
        self.assertIn("ask again in", said)
        self.assertIn("five minutes", said.lower())
        self.assertEqual(len(alonetime.REQUESTS), 1, "the second ask changed nothing")

    async def test_the_holder_allows_it_and_the_cap_rises(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self._ask(cog, guild, stranger)
        message = type("Msg", (), {"id": holder.dm_ids[-1]})()
        answer = _Interaction(guild, holder, message=message)
        await cog.answer_request(answer, allow=True)
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertTrue(lock.admits(8))
        self.assertEqual(guild.room.user_limit, 2, "a guest nobody can let past the cap")
        self.assertIn("may join", _said(answer))
        self.assertIsNone(alonetime.request_for(message.id), "one answer per request")

    async def test_keep_out_puts_nobody_on_the_list(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self._ask(cog, guild, stranger)
        message = type("Msg", (), {"id": holder.dm_ids[-1]})()
        answer = _Interaction(guild, holder, message=message)
        await cog.answer_request(answer, allow=False)
        lock = alonetime.lock_for(guild.id, guild.room.id)
        self.assertFalse(lock.admits(8))
        self.assertEqual(guild.room.user_limit, 1)
        self.assertIn("not told", _said(answer))
        self.assertIsNone(alonetime.request_for(message.id))

    async def test_only_the_holder_can_answer(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self._ask(cog, guild, stranger)
        message = type("Msg", (), {"id": holder.dm_ids[-1]})()
        meddler = _Member(9, "Meddler", guild=guild, perms=_Perms(manage_channels=True))
        answer = _Interaction(guild, meddler, message=message)
        await cog.answer_request(answer, allow=True)
        self.assertIn("Only the holder", _said(answer))
        self.assertFalse(alonetime.lock_for(guild.id, guild.room.id).admits(8))
        self.assertIsNotNone(
            alonetime.request_for(message.id), "a refused press is not an answer"
        )

    async def test_a_guest_asking_to_join_is_told_they_already_can(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await self._locked(guild, guests=(8,))
        guest = _Member(8, "friend", guild=guild)
        interaction = await self._ask(cog, guild, guest)
        self.assertIn("already allowed in", _said(interaction))

    async def test_the_holder_asking_for_their_own_room_is_pointed_at_the_picker(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        interaction = await self._ask(cog, guild, holder)
        self.assertIn("picker on your card", _said(interaction))

    async def test_an_open_room_is_not_a_request_at_all(self):
        guild = self._guild()
        cog = await self._cog(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        interaction = _Interaction(guild, stranger)
        await cog.ask_to_join(interaction, room=guild.elsewhere)
        self.assertIn("not locked any more", _said(interaction))

    async def test_quiet_hours_has_nobody_to_ask(self):
        guild = self._guild()
        cog = await self._cog(guild)
        await alonetime.lock_all_voice(guild, minutes=5)
        stranger = _Member(8, "Nosey", guild=guild)
        interaction = await self._ask(cog, guild, stranger)
        self.assertIn("quiet hours", _said(interaction))

    async def test_an_expired_request_cannot_be_answered(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stale = _Sent()
        alonetime.remember_request(
            stale.id,
            alonetime.Request(
                guild_id=guild.id, channel_id=guild.room.id, requester_id=8,
                requester_name="Nosey", holder_id=holder.id,
                asked_at=time.time() - alonetime.REQUEST_TTL_SECONDS - 5,
            ),
        )
        answer = _Interaction(guild, holder, message=stale)
        await cog.answer_request(answer, allow=True)
        self.assertIn("expired", _said(answer))
        self.assertIsNone(alonetime.request_for(stale.id))

    async def test_the_notice_button_knows_which_room_it_is_about(self):
        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        notice_id = house.sent_ids[-1]
        self.assertEqual(alonetime.ask_card_room(notice_id), guild.room.id)
        interaction = _Interaction(guild, stranger)
        await cog.ask_to_join(interaction, notice_message_id=notice_id)
        self.assertTrue(holder.dms, "the button delivered the same request")

    async def test_a_holder_who_cannot_be_dmed_gets_the_notice_channel(self):
        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        holder = await self._locked(guild)

        async def refuse(**_kwargs):
            raise RuntimeError("DMs are closed")

        holder.send = refuse
        stranger = _Member(8, "Nosey", guild=guild)
        interaction = await self._ask(cog, guild, stranger)
        card = _card_text(house)
        self.assertIn("DMs are closed to me", card, "the fallback says why it is public")
        self.assertIn("Nosey", card)
        self.assertIn("Your request is with", _said(interaction))

    async def test_a_release_drops_every_request_for_the_room(self):
        guild = self._guild()
        cog = await self._cog(guild)
        holder = await self._locked(guild)
        stranger = _Member(8, "Nosey", guild=guild)
        await self._ask(cog, guild, stranger)
        self.assertTrue(alonetime.REQUESTS)
        await alonetime.release_channel(guild, alonetime.lock_for(guild.id, guild.room.id))
        self.assertEqual(alonetime.REQUESTS, {}, "nothing outlives the session")


class TheTypographicCards(_CogCase):
    """The revamp, held to what it removed: no card this feature draws carries
    a text emoji. The one symbol that stays is the mark on the room's own name,
    which is where Discord's sidebar needs it — and the cards print the name
    without it, because the card itself already says the room is locked."""

    _EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")

    def test_a_locked_rooms_name_is_printed_without_the_mark(self):
        self.assertEqual(alonetime.plain_name("🔒 Study"), "Study")
        self.assertEqual(alonetime.plain_name("Lobby"), "Lobby")
        self.assertEqual(alonetime.plain_name(""), "")

    async def test_no_card_carries_a_text_emoji(self):
        from cogs.alonetime import AloneTime

        guild = self._guild()
        cog = await self._cog(guild)
        house = guild.add_text_channel("bot-commands")
        holder = _Member(7, "rek_ter", guild=guild)
        guild.members.append(holder)
        holder.voice.channel = guild.room
        private = _Interaction(guild, holder)
        await AloneTime.alone_start.callback(cog, private, "reading", None)
        stranger = _Member(8, "Nosey", guild=guild)
        await cog._voice_event(stranger, _state(guild.elsewhere), _state(guild.room))
        status = _Interaction(guild, holder)
        await AloneTime.alone_status.callback(cog, status)
        for label, text in (
            ("the holder's card", _card_of(private)),
            ("the public notice", _card_text(house)),
            ("the status card", _card_of(status)),
        ):
            self.assertTrue(text, f"{label} should have said something")
            self.assertIsNone(self._EMOJI.search(text), f"{label} carries an emoji")
        # The turned-away member's card *is* the public notice above — it lands in
        # the house channel, and a voice channel's own chat must carry nothing.
        # That reroute is this feature's promise, so the empty chat is a law here
        # rather than an omission: the landing room is where the member was put,
        # and notices were never for the room, they were for the people.
        self.assertEqual(
            _card_text(guild.elsewhere), "",
            "a voice channel's chat must carry no notice",
        )
        self.assertIn("🔒", guild.room.name, "the sidebar mark is not a card")

    async def test_the_notice_routing_is_a_setting_on_the_card(self):
        spec = features.get_setting("alonetime", "notice_channel")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.kind, "text")
        self.assertEqual(spec.default, "")

    async def test_the_help_menu_names_the_request_command(self):
        from cogs import help as help_cog

        self.assertIn("/alone ask", help_cog.HELP_MODULES["alonetime"]["desc"])


class TheOneThingItCannotDo(unittest.TestCase):
    """Activity sharing is the member's own setting, and the cards say so."""

    def test_the_status_reading_says_activity_sharing_is_not_a_channels_business(self):
        lock = alonetime.Lock(guild_id=1, channel_id=2, holder_id=7, holder_name="rek_ter")
        rows = dict(alonetime.status_lines(lock, alonetime.Settings()))
        self.assertIn("Activity Sharing", rows["Privacy"])
        self.assertIn("no bot can change it", rows["Privacy"])

    def test_the_help_copy_says_it_too(self):
        from cogs import help as help_cog

        desc = help_cog.HELP_MODULES["alonetime"]["desc"]
        self.assertIn("Activity Sharing", desc)
        self.assertIn("no bot can change it", desc)


if __name__ == "__main__":
    unittest.main()
