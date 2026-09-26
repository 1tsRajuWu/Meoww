"""What one member can run here, grouped the way the registry groups it.

Three questions arrive at this module at once, and it answers all three from the
same two tables — because answering any of them somewhere else is how two
surfaces come to disagree about the same server:

* **which commands exist.** The live command tree, read through
  :func:`utils.permissions.walk` so a group's subcommands are rows and the group
  itself is not: ``/market buy`` is something a person types, ``/market`` is not.
* **what each one belongs to.** ``utils.features`` owns this, twice over: the
  gate says which systems must be on, and the feature that declares the row owns
  the sentence printed after it — ``/market buy — Buy an instrument with coins``
  is one string in the registry and this module is where the two halves part.
* **who may run it.** ``utils.permissions.COMMAND_ACCESS`` and the
  ``PICKER_VISIBILITY`` map beside it — the very table Discord's picker is
  stamped from before the tree is published (``apply_declared_permissions``). So
  the list a member sees on the ``/cmd`` card and the list in Discord's picker
  are computed from one table rather than agreeing by hand.

**Nothing is withheld silently.** Every row this module will not offer comes
back carrying the reason, because a bare "no results" is the answer this project
has already paid for once: a member searching for ``ban`` has asked a real
question, and the honest reply names what exists and why it is not theirs to run
rather than pretending the bot has nothing.

**Where the picker is wider than the truth, the row says so.** Discord's
permission bits cannot express "the server owner", "the operator" or "whoever
wrote the quote" — so four audiences are stamped with a bit that *approximates*
them (:data:`utils.permissions.SOURCE_MARKER` names the check each one really
runs). For the owner this module evaluates the real check, because the caller
can: ``guild.owner_id`` is at hand. For the rest the row is offered the way
Discord offers it and carries a caveat — "operator only", "the person who saved
it" — so the card never claims more than the command will honour.

Pure, like :mod:`utils.command_scope`: no client, no database, no I/O. The
caller hands it a :class:`Viewer` (the module states for one server, and one
member's permissions, roles and owner-ness, gathered once) and reads the answer
back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from utils import features, market as market_rules, permissions

#: The group for commands no system gates — the front door itself.
CORE_ID = "core"
CORE_NAME = "Always available"
CORE_ICON = "🧭"

# --------------------------------------------------------------------------- #
# Why a row is not offered
# --------------------------------------------------------------------------- #
#: The reason kinds, in the order the card lists them: a switch an administrator
#: can flip, then a permission somebody can be granted, then the two audiences
#: nobody can be granted into, then the market's role.
OFF = "off"
PERMISSION = "permission"
OWNER = "owner"
OPERATOR = "operator"
MARKET = "market"

#: Reason kinds that may not be *named* to somebody Discord would not offer them
#: to. ``utils/permissions`` states the rule for the operator's own commands in
#: its own words — a member reading a name they may not have is a leak — and this
#: is the same sentence applied to a listing rather than to a picker. A count
#: survives; the name does not.
PRIVATE_REASONS = frozenset({OPERATOR})


def _describe(permission: str) -> str:
    """``moderate_members`` -> ``Moderate Members``."""
    return permission.replace("_", " ").title()


def _bits(required) -> tuple[str, ...]:
    """The permission names a :class:`discord.Permissions` requires.

    Every audience in the table asks for exactly one bit, and a test pins that —
    which is what makes "the member must hold *this*" an unambiguous sentence. The
    iteration is discord.py's own, so a bit added to the table cannot be missed by
    a hand-written list of names here.
    """
    named = []
    try:
        pairs = list(required)
    except TypeError:  # pragma: no cover - a stand-in with only .value
        return ()
    for name, value in pairs:
        if value:
            named.append(_describe(name))
    return tuple(named)


@dataclass(frozen=True)
class Reason:
    """Why a command is not offered, and whether its name may be printed."""

    kind: str
    text: str

    @property
    def private(self) -> bool:
        return self.kind in PRIVATE_REASONS


# --------------------------------------------------------------------------- #
# One command
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Command:
    """One row somebody can type, and everything needed to describe it."""

    path: str
    root: str
    system_id: str
    system: str
    icon: str
    note: str
    audience: str

    @property
    def text(self) -> str:
        """How it is written in a message: ``/market buy``."""
        return f"/{self.path}"


@dataclass(frozen=True)
class Group:
    """A system, and every command of it this member may run."""

    id: str
    name: str
    icon: str
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class Held:
    """A command that is not offered, and the reason."""

    command: Command
    reason: Reason


@dataclass(frozen=True)
class Scoped:
    """One member's answer: what they may run, and what they may not."""

    total: int
    usable: tuple[Command, ...]
    withheld: tuple[Held, ...]

    def count(self, kind: str) -> int:
        return sum(1 for held in self.withheld if held.reason.kind == kind)

    @property
    def order(self) -> tuple[str, ...]:
        """The reason kinds present, in the card's reading order."""
        return tuple(kind for kind in (OFF, PERMISSION, OWNER, OPERATOR, MARKET) if self.count(kind))


@dataclass(frozen=True)
class Viewer:
    """Everybody the two tables need to know about, gathered once."""

    #: Module state for one server, from ``utils.modules.get_states``.
    states: Mapping[str, bool] = field(default_factory=dict)
    #: The member's own ``guild_permissions``.
    permissions: object | None = None
    #: ``guild.owner_id == user.id``, or the bot's owner.
    is_owner: bool = False
    #: The member's role ids, and the server's — for the market's role gate.
    role_ids: frozenset = frozenset()
    guild_role_ids: frozenset = frozenset()

    def holds(self, required) -> bool:
        """Whether this member clears a ``default_member_permissions`` value.

        Discord's rule, and its order matters: an administrator holds everything
        (which is why ``OWNER`` and ``DEV`` are declared as administrator — the
        picker has no "is the owner" bit), and otherwise **every** bit asked for
        has to be present. ``is_subset`` in the library rather than ``&`` here
        would be the same answer, but the bit arithmetic reads as the rule: all of
        them, which is true even if a future audience ever names two.
        """
        if required is None:
            return True
        if getattr(self.permissions, "administrator", False):
            return True
        have = int(getattr(self.permissions, "value", 0) or 0)
        want = int(getattr(required, "value", 0) or 0)
        return (have & want) == want


# --------------------------------------------------------------------------- #
# Reading the tree
# --------------------------------------------------------------------------- #
def _system_home() -> dict[str, str]:
    """Root command name -> the system that *declares* it, for ungated commands.

    Only ever consulted for a command the gate table does not know: ``/bot sync``
    and ``/legal privacy`` are core, and the registry still lists them under
    *System & Server Control* — so that is where a directory puts them, rather
    than inventing a bucket of its own. Derived rather than typed, so retiring a
    row moves it off the card with the registry edit that retired it.
    """
    home: dict[str, str] = {}
    for feature in features.FEATURES:
        for entry in feature.commands:
            root = str(entry).strip().lstrip("/").split()[0].lower()
            if root:
                home.setdefault(root, feature.id)
    return home


def _notes() -> dict[str, str]:
    """Command path -> the registry's own sentence about it, where it wrote one."""
    notes: dict[str, str] = {}
    for feature in features.FEATURES:
        for name, note in features.feature_entries(feature):
            if not note:
                continue
            notes.setdefault(name.lstrip("/").lower(), note)
    return notes


def commands(tree) -> tuple[Command, ...]:
    """Every command in ``tree``, in registry order, as rows.

    The tree is the authority on what *exists* — a row the registry declares but
    Discord was never told about is not a command anybody can type, and the other
    way round is a command that plainly is one. The registry supplies the note and
    the system; where it knows nothing about a row the row is still listed, in the
    core group, which is the same failing-open rule :mod:`utils.command_scope`
    applies and for the same reason: a listing may not lose a command.
    """
    notes = _notes()
    home = _system_home()
    rows: list[Command] = []
    for path, _command in permissions.walk(tree):
        root = path.split()[0]
        gated = features.MODULE_COMMAND_MAP.get(root, ())
        if gated:
            system_id = gated[0]
        else:
            system_id = home.get(root.lower(), CORE_ID)
        feature = features.get(system_id)
        shared = gated[1:]
        note = notes.get(path.lower(), "")
        if shared:
            # A command two systems gate is listed under the first and *says* so:
            # a member who opens Mini-Games and does not see /slots would
            # otherwise conclude it had been retired.
            others = ", ".join(
                name for name in (getattr(features.get(fid), "name", fid) for fid in shared)
            )
            note = f"{note} *(needs {others} as well)*" if note else f"*(needs {others} as well)*"
        rows.append(
            Command(
                path=path,
                root=root,
                system_id=system_id,
                system=feature.name if feature else CORE_NAME,
                icon=feature.icon if feature else CORE_ICON,
                note=note,
                audience=permissions.COMMAND_ACCESS.get(path, permissions.EVERYONE),
            )
        )
    return tuple(rows)


def declared_but_absent(tree) -> tuple[str, ...]:
    """Registry rows no command in the tree answers to.

    Empty in a healthy tree, and worth a test of its own: a declared row that is
    not a command is a row the directory would have printed for nobody, which is
    the failure this project has twice shipped as "the page offers a command that
    answers *unknown command*".
    """
    present = {path.lower() for path, _command in permissions.walk(tree)}
    declared = {name.lstrip("/").lower() for name, _note in features.command_entries()}
    return tuple(sorted(declared - present))


# --------------------------------------------------------------------------- #
# Reading the table
# --------------------------------------------------------------------------- #
def caveat(command: Command, viewer: Viewer) -> str:
    """A short qualifier for a row Discord offers more widely than the command does.

    Three audiences have no permission bit — the operator, the server owner and
    whoever wrote a record — and for one of them the *picker* is stamped with
    Administrator anyway, so Discord itself shows the row to administrators who
    will be refused at use time. Where that is true and this member clears the
    bit, the row is offered **with this sentence**, which is the difference
    between a directory that is useful and one that lies.
    """
    if command.audience == permissions.DEV:
        return "operator only"
    if command.audience == permissions.SUBMITTER:
        return "whoever saved it, or staff"
    return ""


def refusal(command: Command, viewer: Viewer) -> Reason | None:
    """Why this member cannot run ``command``, or ``None`` when they can.

    Order matters. A system switched off is a fact about the *server* and the
    first thing an administrator can undo, so it is checked before the member's
    own permissions: telling somebody at a moderated server "you need Ban Members"
    when the moderation system is off would send them to ask for the wrong thing.
    """
    if not all(
        bool(viewer.states.get(module, features.default_enabled(module)))
        for module in features.MODULE_COMMAND_MAP.get(command.root, ())
    ):
        return Reason(OFF, f"the **{command.system}** system is switched off here")
    if command.audience == permissions.OWNER and not viewer.is_owner:
        return Reason(OWNER, "only the server owner can run this")
    if command.audience == permissions.MARKET:
        problem = market_rules.access_problem(
            viewer.role_ids, guild_role_ids=viewer.guild_role_ids
        )
        if problem is not None:
            text = (
                "the market role is not on this server yet"
                if problem == "missing"
                else "needs the server's market role"
            )
            return Reason(MARKET, text)
    if command.audience == permissions.DEV and not viewer.holds(
        permissions.PICKER_VISIBILITY[permissions.DEV]
    ):
        return Reason(OPERATOR, "only the operator can run this")
    required = permissions.PICKER_VISIBILITY.get(command.audience)
    if not viewer.holds(required):
        named = _bits(required)
        # ``get`` and not ``[...]``: an audience nobody declared (a command that
        # skipped the table) has no requirement, and the honest answer for one is
        # that it needs *something* rather than a made-up bit. ``tests/
        # test_permissions.py`` is what refuses to let that happen at all.
        text = f"needs **{' and '.join(named)}**" if named else "needs a permission you do not have"
        return Reason(PERMISSION, text)
    return None


def scope(rows: Iterable[Command], viewer: Viewer) -> Scoped:
    """Split every row into what this member may run and what they may not."""
    usable: list[Command] = []
    withheld: list[Held] = []
    total = 0
    for command in rows:
        total += 1
        reason = refusal(command, viewer)
        if reason is None:
            usable.append(command)
        else:
            withheld.append(Held(command, reason))
    return Scoped(total=total, usable=tuple(usable), withheld=tuple(withheld))


# --------------------------------------------------------------------------- #
# Grouping and searching
# --------------------------------------------------------------------------- #
def grouped(rows: Iterable[Command]) -> tuple[Group, ...]:
    """The rows, by system, in registry order — with the core group last.

    Registration order inside a group is kept rather than sorted: it is the order
    the cog declares its own commands in, which is the order the commands were
    designed to be read in ("/deposit then /withdraw", not an alphabet).
    """
    order = [feature.id for feature in features.FEATURES] + [CORE_ID]
    buckets: dict[str, list[Command]] = {key: [] for key in order}
    for command in rows:
        buckets.setdefault(command.system_id, []).append(command)
    groups: list[Group] = []
    for key in order:
        held = buckets.get(key) or []
        if not held:
            continue
        first = held[0]
        groups.append(Group(id=key, name=first.system, icon=first.icon, commands=tuple(held)))
    return tuple(groups)


def _field_rank(command: Command, token: str) -> int | None:
    """How well one word matches one row — or ``None`` for no match at all.

    The ranks are the card's reading order for a result list: an exact name, the
    group's name, a name that starts with the word, a name that contains it, the
    registry's description, and last the system it lives in — so "casino" finds
    the whole games system while "slots" finds one command.
    """
    path = command.path.lower()
    if path == token:
        return 0
    if command.root == token:
        return 1
    if path.startswith(token):
        return 2
    if token in path:
        return 3
    if token in command.note.lower():
        return 4
    if token in command.system.lower():
        return 5
    return None


def match(command: Command, query: str) -> int | None:
    """A row's score against a search, or ``None`` when it does not match.

    Every word has to land somewhere — in the name, the registry's sentence or
    the system — and the score is the *worst* of those landings, so a row that
    matches "market" exactly and "buy" only in its description sorts below one
    whose name holds both. A leading slash is thrown away, so the thing a member
    copied out of a card ("/market buy") is a valid search here.
    """
    tokens = [token for token in str(query or "").replace("/", " ").lower().split() if token]
    if not tokens:
        return None
    ranks = [_field_rank(command, token) for token in tokens]
    if any(rank is None for rank in ranks):
        return None
    return max(ranks)  # type: ignore[arg-type]


def search(rows: Iterable[Command], query: str) -> tuple[Command, ...]:
    """The rows matching ``query``, best first and stable within a rank."""
    scored = []
    for command in rows:
        rank = match(command, query)
        if rank is not None:
            scored.append((rank, command))
    scored.sort(key=lambda pair: (pair[0], pair[1].path))
    return tuple(command for _rank, command in scored)
