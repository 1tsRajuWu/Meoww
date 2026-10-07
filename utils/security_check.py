"""The holes a server cannot see from Discord's own screen.

`/check` exists because the same class of problem has now been reported from the
other side twice — *a channel that reads as private and is not*, *a lock that reads
as locked and is not* — and neither is visible anywhere in Discord's interface: the
deny on `@everyone` is there, and the **allow** that beats it sits on a different
overwrite of the same channel, on a screen Discord draws one overwrite at a time.
*Who can actually see this room* has no page, which is why the answer has to be
computed.

Four decisions shape this module, and each one is a way a scan like this goes
wrong:

* **A finding is a fact, never a guess.** Every check reads objects Discord already
  sent — the overwrites, the role bits, the bot's own permissions. The one question
  that needs the database (which channels a lockdown holds) is asked of
  :mod:`utils.server_control`, which owns that ledger, and the one that needs a
  setting (the moderation log) is asked of :mod:`utils.modlog`. Nothing here changes
  anything: `/check` is a read, and the single action it can take is the one the
  caller asks for by name (`fix`), through the same door `/lockdown` uses.
* **The rule the whole thing exists for is one line of Discord's own model.** A
  role's or a member's channel overwrite is applied *after* `@everyone`'s, so an
  allow there beats a deny — which is why `@everyone view_channel=False` reads as
  private and is not, and why a locked channel can still be typed in. Both checks ask
  :func:`utils.server_control.leaks` — the same function the lock itself uses — so
  the report and the lock can never disagree about what a leak is.
* **Critical is a hole, not a preference.** Something is critical when it lets a
  person past a permission the server believes it set: a lock that can be typed
  through, `@everyone` holding Manage Roles (which is Administrator with extra
  steps), a channel where `@everyone` may manage webhooks. A missing setting or a
  switched-off system is a warning; who holds power, what the server has not
  switched on, and **who can actually see each private channel** are notes — the
  last one because Discord's own answer to *who can see this* is a permission screen
  per channel, which is the screen nobody opens.
* **Nothing is reported twice, and a channel the ledger holds is the lock's
  business.** One channel that is private-and-granted is one finding with the roles
  named in the sentence, not one finding per role; a channel in the lockdown ledger
  — hidden or not — is answered by the lock check and left out of the visibility
  list, because *who can see it* is exactly the question the repair acts on.
* **The deny can live on the category, and Discord does not apply it to a room
  that was edited.** Permissions are computed *from the channel*; the category is
  the template a channel is copied from, which is Discord's own word for it —
  *Permission Syncing*: a channel with overwrites of its own is **de-synced**, and
  "its permissions will no longer change with changes to its parent category". So a
  category that denies `@everyone` **View Channel** hides every room that is
  synced to it and **none of the rooms that carry an `@everyone` overwrite of their
  own** — and the second shape is the one Discord's panel draws as a red X on the
  category while the room is open to everybody. It is a finding, not a note, because
  the server believes it made those rooms private. The reader asks the room's own
  overwrite for that bit, so the room that *does* carry the category's deny — a
  `➕ Create A VC` that was given the mask by hand — is correctly quiet.

Pure over a guild apart from the two reads it deliberately borrows, so a law can
drive it with fakes and the card can be asserted word for word.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db, modlog, server_control

#: The levels, worst first: the order the card draws them and the order to read.
CRITICAL = "critical"
WARNING = "warning"
NOTE = "note"
LEVELS: tuple[str, ...] = (CRITICAL, WARNING, NOTE)

#: What each level is called on the card. The headings say what the level *means*
#: rather than how bad it sounds, because "critical" next to a missing permission
#: teaches a reader to ignore the word.
LEVEL_TITLES: dict[str, str] = {
    CRITICAL: "🔴 Open right now",
    WARNING: "🟠 Broken or missing",
    NOTE: "🔵 Worth knowing",
}

#: What `@everyone` must not hold, and what it means when it does. The rule is
#: *a permission that hands over the server, or lets somebody act as it* rather than
#: a list of scary-sounding bits: `send_messages` on `@everyone` is the normal state
#: of a community and is not here at all, and neither is `add_reactions`.
EVERYONE_POWER: tuple[tuple[str, str, str], ...] = (
    ("administrator", CRITICAL, "every permission there is, and every channel overwrite — including a lockdown"),
    ("manage_roles", CRITICAL, "any role below its own, which is every role — so it can hand itself administrator"),
    ("manage_channels", CRITICAL, "edit or delete the channels a lockdown is holding"),
    ("manage_guild", CRITICAL, "edit the server itself, its integrations and its vanity address"),
    ("manage_webhooks", CRITICAL, "post as any name and avatar it likes through a webhook"),
    ("ban_members", WARNING, "remove members for good"),
    ("kick_members", WARNING, "remove members"),
    ("moderate_members", WARNING, "time out anybody below their highest role"),
    ("mention_everyone", WARNING, "ping the whole server whenever it wants"),
    ("create_instant_invite", WARNING, "invite anybody, including an account made a minute ago"),
    ("manage_expressions", NOTE, "add, rename or delete the server's emoji and stickers"),
)

#: A channel overwrite can grant most of the above to `@everyone` in one room — the
#: notes are left out here, because a note per channel is noise and the fast invite is
#: a server-level decision rather than a room's.
CHANNEL_POWER: tuple[tuple[str, ...], ...] = tuple(
    (name, level, detail) for name, level, detail in EVERYONE_POWER if level != NOTE
)

#: What the moderation half of this bot needs at the server level, and what stops
#: working without it. Written as the *symptom* rather than the permission, because
#: the permission is already in the sentence above it.
BOT_NEEDS: tuple[tuple[str, str], ...] = (
    ("manage_channels", "`/lockdown` cannot change a channel's permissions at all"),
    ("manage_messages", "the sweep cannot remove what is posted into a locked channel"),
    ("moderate_members", "a spammer in a locked channel cannot be timed out"),
    ("manage_roles", "`/addrole`, `/removerole` and `/defaultrole` cannot assign anything"),
    ("view_audit_log", "a lockdown older than the ledger cannot be adopted, so the sweep walks past it"),
    ("kick_members", "`/kick` is refused"),
    ("ban_members", "`/ban` is refused"),
    ("send_messages", "cards cannot be posted here at all"),
)

#: The text channels the scan walks, by Discord's own type names. Threads are left
#: out on purpose: a thread's permissions are its parent's, so a hole in the parent is
#: reported where it lives, and a thread cannot be made private on its own.
CHANNEL_KINDS: tuple[str, ...] = ("text", "news", "forum", "voice", "stage", "category")

#: How many rows a level may draw before the card says *and N more*. A card is not a
#: console, and a server with forty private channels must still get a readable answer.
ROWS_PER_LEVEL = 6


@dataclass
class Finding:
    """One hole, in the words the card prints."""

    level: str
    title: str
    detail: str = ""
    where: str = ""

    def line(self) -> str:
        head = f"{self.where} — **{self.title}**" if self.where else f"**{self.title}**"
        return f"{head}\n{self.detail}" if self.detail else head


@dataclass
class Audit:
    """What the scan found, and what a `fix` may put right."""

    guild_id: int = 0
    findings: list = field(default_factory=list)
    #: Locked channels whose deny can be beaten — the repair `fix` makes first.
    beaten: list = field(default_factory=list)
    #: Locked channels whose deny is **gone** but whose row stayed: nothing reads as
    #: locked and the sweep still removes what is posted. `fix` writes the deny again.
    loose: list = field(default_factory=list)
    #: Channels an old lockdown left its overwrite on with no row, in exactly the
    #: shape the adoption pass will **consider** — the ones it is allowed to ask the
    #: audit log about. The card's repair sentence is built from it, because
    #: *nothing needed repairing* beside a screen still full of unenforced locks is
    #: read as a bug, and *nothing I may adopt* is a different answer.
    orphans: list = field(default_factory=list)
    #: Rooms carrying the **exact** mark an old `/unlock` left — *allow Send
    #: Messages, deny nothing* on `@everyone` — where removing it is an undo.
    #: This is the one hole a `fix` may undo rather than re-lock, and it can undo it
    #: because the room had no overwrite of its own before the bug wrote one:
    #: removing it is what puts the room back under its category. Built from the
    #: same predicate the pass itself asks (:func:`stray_unlock_is_undoable`), so the
    #: rooms the card promises to put back are the rooms it puts back.
    strays: list = field(default_factory=list)
    #: Channels walked, so *no holes* can say what it looked at.
    checked: int = 0
    #: Checks that could not be read (a missing ledger, a guild without a member
    #: cache). Named rather than swallowed: a scan that answered *nothing found*
    #: because it could not read is the failure this whole module is written against.
    unread: int = 0

    def of(self, level: str) -> list:
        return [finding for finding in self.findings if finding.level == level]

    @property
    def counts(self) -> dict:
        return {level: len(self.of(level)) for level in LEVELS}

    @property
    def worst(self) -> str:
        """The worst level present, or ``\"\"`` when the server is clean."""
        for level in LEVELS:
            if self.of(level):
                return level
        return ""

    @property
    def ok(self) -> bool:
        return not self.findings

    def sentence(self) -> str:
        if self.ok:
            said = f"No holes found in **{self.checked}** channel(s)."
        else:
            gaps = ", ".join(
                f"**{self.counts[level]}** {level}" for level in LEVELS if self.counts[level]
            )
            said = f"**{len(self.findings)}** finding(s) across **{self.checked}** channel(s): {gaps}."
        if self.unread:
            said += f" {self.unread} check(s) could not be read."
        return said

    def to_dict(self) -> dict:
        return {
            "guild_id": str(self.guild_id),
            "ok": self.ok,
            "checked": self.checked,
            "unread": self.unread,
            "counts": self.counts,
            "said": self.sentence(),
            "findings": [
                {"level": f.level, "where": f.where, "title": f.title, "detail": f.detail}
                for f in self.findings
            ],
        }


def channels_of(guild) -> list:
    """Every channel a permission can hide, from the types this scan walks."""
    found = []
    for channel in list(getattr(guild, "channels", None) or []):
        kind = str(getattr(channel, "type", "")).lower()
        if any(name in kind for name in CHANNEL_KINDS):
            found.append(channel)
    return found


def _bot_id(guild) -> int:
    return int(getattr(getattr(guild, "me", None), "id", 0) or 0)


def _label(target) -> str:
    return f"@{getattr(target, 'name', '?') or '?'}"


def _whose(targets, *, limit: int = 4) -> str:
    """The targets of a leak as one clause: ``@Normal``, or five of them and a count."""
    named = [_label(target) for target in targets]
    if len(named) > limit:
        return ", ".join(named[:limit]) + f" and {len(named) - limit} more"
    return ", ".join(named)


def _everyone_of(channel):
    """`@everyone`'s ``(allow, deny)`` on this channel, through the lock's own reader."""
    return server_control.overwrite_pair(server_control.everyone_overwrite(channel))


def claims_private(channel) -> bool:
    """Whether `@everyone` is denied **View Channel** here: Discord's own *private*.

    The bit the report this command came from was about, read the way every other
    overwrite is read — through the lock engine's pair reader, so the audit and the
    lock cannot disagree about which channels are private.
    """
    _allow, deny = _everyone_of(channel)
    return bool(deny is not None and getattr(deny, "view_channel", False))


def category_of(channel):
    """The category this channel sits in, or ``None``.

    Read as the attribute Discord puts on every channel rather than by walking the
    guild: a stand-in in a test without one is a channel in no category, and a
    channel in no category is a channel with nothing above it to be hidden by.
    """
    return getattr(channel, "category", None)


def speaks_for_itself(channel) -> bool:
    """Whether this room's **own** overwrites are what Discord applies to `@everyone`.

    A room with an `@everyone` overwrite of its own is the *de-synced* case, and it
    is the whole reason the category's deny is not a fact about the room: Discord
    computes a channel from the channel, and the category is the template it was
    copied from (its own documentation, *Permission Syncing* — which is also why
    the client's **Sync with category** button copies the category's permissions
    *into* the room rather than the other way round). A room with no `@everyone`
    overwrite of its own is the synced case, and the category's deny *is* applied
    to it — which is why `#general` beside a hidden category is quiet.
    """
    return server_control.everyone_overwrite(channel) is not None


def hidden_by_its_category(channel) -> bool:
    """Whether a category hides this room and the room itself does not.

    The hole one level up from :func:`claims_private`, and the same sentence: *a
    room that reads as private and is not*. Both halves are asked of the readers
    the lock writes through, so the audit and the engine cannot disagree about
    which rooms a category actually hides.
    """
    parent = category_of(channel)
    if parent is None or not claims_private(parent):
        return False
    return speaks_for_itself(channel) and not claims_private(channel)


def _keeps(channel) -> str:
    """What `@everyone` can still do in a room its category was meant to hide.

    Named in the client's own vocabulary, because the card is read beside Discord's
    screens: the red X on the category and the green check on the room, with
    nothing anywhere saying that only the second one applies.
    """
    allow, _deny = _everyone_of(channel)
    held = ["View Channels"]
    for name, label in (
        ("send_messages", "Send Messages"),
        ("add_reactions", "Add Reactions"),
        ("create_public_threads", "Create Public Threads"),
        ("manage_webhooks", "Manage Webhooks"),
        ("create_instant_invite", "Create Invite"),
    ):
        if bool(getattr(allow, name, False)):
            held.append(label)
    return ", ".join(held)


def hidden_rooms(channels, guild=None) -> list:
    """Rooms a category hides and the rooms themselves do not — `@everyone` gets in.

    Each one is a **critical** finding rather than a note, and the level is the
    module's own definition of it: the server set a permission it believes is in
    force, and this room never had it. What the card says about the repair is what
    actually repairs it: a room carrying an old `/unlock`'s own mark is put back
    under its category by `fix`, which removes that overwrite; anything else is the
    repair Discord itself offers — **Sync with category** on the room, or the same
    deny written on the room's own `@everyone` overwrite — because that is what
    makes the deny a fact about the room rather than about its parent.
    """
    found = []
    for channel in channels:
        if not hidden_by_its_category(channel):
            continue
        parent = category_of(channel)
        if _is_an_old_unlock(channel):
            # The one case the bot's own pass can repair: the overwrite that made
            # this room speak for itself is *its* old unlock's, so removing it
            # returns the room to the state it was in before the bot wrote it.
            # Asked of the guild when the caller has one, because on a server whose
            # `@everyone` cannot send at all that overwrite is this room's only
            # source of the permission, and the pass refuses to take it away — a
            # promise the repair cannot keep is the dead end this command was
            # written against.
            if guild is not None and not server_control.everyone_can_send(guild):
                repair = (
                    "That overwrite is **exactly** the one an old `/unlock` of mine "
                    "left — *allow Send Messages, deny nothing* — and `@everyone` "
                    "can send in this room *only* through it, so I will not remove "
                    "it: **Sync with category** on the room makes the deny hold "
                    "without silencing anybody."
                )
            else:
                repair = (
                    "That overwrite is **exactly** the one an old `/unlock` of mine "
                    "left — *allow Send Messages, deny nothing* — so `/check fix:True` "
                    "removes it and this room takes its category's permissions again."
                )
        else:
            repair = (
                "**Sync with category** on the room, or deny `View Channel` on the "
                "room's own `@everyone` overwrite, is what makes the deny hold."
            )
        found.append(
            Finding(
                level=CRITICAL,
                where=f"#{getattr(channel, 'name', '?')}",
                title=(f"is hidden by its category **#{getattr(parent, 'name', '?')}** and is not"),
                detail=(
                    "Discord computes a channel's permissions **from the channel**: "
                    "the category is the template it was copied from, and a room that "
                    "has overwrites of its own is de-synced — its own documentation "
                    "says such a room stops taking the category's permissions, which is "
                    "why the client's *Sync with category* copies the category's "
                    "permissions **into** the room. The deny is on the category and "
                    f"`@everyone` keeps **{_keeps(channel)}** here, because that is what "
                    f"the server's default permissions leave. {repair}"
                ),
            )
        )
    return found


def stray_marks(guild, channels) -> list:
    """Rooms an old `/unlock` de-synced where **nothing is hidden yet** — the next bug.

    A room the category currently hides is :func:`hidden_rooms`' critical finding, and
    the same overwrite is what the next complaint will be about: Discord applies a
    category's permissions to a room that has none of its own, an overlay is what
    stops that being true, and this one is the bot's own leftover. On the server this
    was written for, five rooms still carry it — `#general`, `#off-topic`,
    `#bot-cmd`, `#color-chan-cmd` and `#control-panel` — which is why hiding `Main`
    or `Voice` one day will quietly not hold, and why the report that asked for this
    module arrived a week after the command that wrote them.

    A **note** rather than a critical row: nothing is open right now that would be
    shut, and the room is exactly as talkable as its category leaves it. What is
    missing is that the category is no longer in charge of it.
    """
    found = []
    for channel in channels:
        if not _is_an_old_unlock(channel):
            continue
        if hidden_by_its_category(channel):
            # Already a critical finding above, with its own repair sentence.
            continue
        parent = category_of(channel)
        if server_control.stray_unlock_is_undoable(guild, channel):
            closing = (
                "Nothing is taken away by removing it here: `@everyone` can already "
                "send server-wide and nothing else is granted, which is why "
                "`/check fix:True` removes it."
            )
        else:
            closing = (
                "`@everyone` can send in this room *only* through it, so I will not "
                "remove it — **Sync with category** on the room makes its category "
                "apply again without silencing anybody."
            )
        consequence = (
            f"the next change to **#{getattr(parent, 'name', '?')}** will not reach "
            "this room"
            if parent is not None
            else "this room will not follow the category it is moved into"
        )
        found.append(
            Finding(
                level=NOTE,
                where=f"#{getattr(channel, 'name', '?')}",
                title="carries an old `/unlock` of mine that was never taken back",
                detail=(
                    "Its `@everyone` overwrite is exactly what that command wrote — "
                    "*allow Send Messages, deny nothing* — and an overwrite of its "
                    "own is what stops a room taking its category's permissions, so "
                    f"{consequence}. {closing}"
                ),
            )
        )
    return found


def _is_an_old_unlock(channel) -> bool:
    """Whether this room's own `@everyone` overwrite is an old `/unlock`'s mark.

    Read through the lock engine's own fingerprint (:func:`is_stray_unlock`), so the
    audit and the undo cannot disagree about which rooms the bug wrote — and so a
    promise the card makes is the promise the repair keeps.
    """
    return server_control.is_stray_unlock(server_control.everyone_overwrite(channel))


def _denies(channel, names) -> bool:
    """Whether `@everyone`'s deny still holds every one of ``names``."""
    _allow, deny = _everyone_of(channel)
    return all(bool(getattr(deny, name, False)) for name in names)


def _leaks(channel, names, bot_id: int) -> list:
    """The overwrites that grant one of ``names``, minus the bot's own.

    The bot's overwrite is not a leak: a ticket channel hands it `View Channel` and
    `Send Messages` on purpose, and the card it posts into a room it has just locked
    needs both.
    """
    return [
        target
        for target in server_control.leaks(channel, names)
        if int(getattr(target, "id", 0) or 0) != bot_id
    ]


def visibility_notes(guild, channels, *, locked=()) -> list:
    """Who can see a channel that `@everyone` cannot — Discord never draws this list.

    This is the direct answer to the report this command came from: somebody made a
    channel private, a role could still see it, and nothing on the screen said so.
    It is a **note** rather than an alarm because a role being let into a private room
    is the normal way a private room is used — what is wrong is never the mechanism,
    it is the role nobody meant to leave in, and only the server's own people can say
    which that is.

    ``locked`` is the channels the lockdown ledger already holds: their visibility is
    the lock's own question, answered by ``lockdown_findings`` with the repair beside
    it, so they are left out here rather than described twice in two different words.
    """
    found = []
    bot_id = _bot_id(guild)
    held = {int(channel_id) for channel_id in locked}
    for channel in channels:
        if int(getattr(channel, "id", 0) or 0) in held:
            continue
        if not claims_private(channel):
            continue
        leaks = _leaks(channel, ("view_channel",), bot_id)
        if not leaks:
            continue
        found.append(
            Finding(
                level=NOTE,
                where=f"#{getattr(channel, 'name', '?')}",
                title=f"private to `@everyone`, open to {_whose(leaks)}",
                detail=(
                    "This is who can actually see it. `/lockdown hide:True` denies "
                    "`View Channel` on every overwrite that grants it, and `/unlock` "
                    "puts them back."
                ),
            )
        )
    return found


def everyone_power(guild) -> list:
    """Permissions `@everyone` holds **for the whole server**."""
    found = []
    everyone = getattr(guild, "default_role", None)
    permissions = getattr(everyone, "permissions", None)
    if everyone is None or permissions is None:
        return found
    for name, level, detail in EVERYONE_POWER:
        if not bool(getattr(permissions, name, False)):
            continue
        found.append(
            Finding(
                level=level,
                where="@everyone",
                title=f"has **{name.replace('_', ' ')}** — {detail}",
                detail=(
                    "Every member of the server has this, whether or not they ever "
                    "post. Take it away in **Server Settings → Roles** unless it is "
                    "genuinely meant for everybody."
                ),
            )
        )
    return found


def channel_power(guild, channels) -> list:
    """The same question read per channel: what `@everyone` may do in one room.

    A channel's own overwrite can hand out what the role does not: `Manage Webhooks`
    in a single channel is enough to post as anybody through a webhook there, and it
    is invisible from the role screen everybody checks.
    """
    found = []
    for channel in channels:
        allow, _deny = _everyone_of(channel)
        if allow is None:
            continue
        for name, level, detail in CHANNEL_POWER:
            if not bool(getattr(allow, name, False)):
                continue
            found.append(
                Finding(
                    level=level,
                    where=f"#{getattr(channel, 'name', '?')}",
                    title=f"`@everyone` may **{name.replace('_', ' ')}** here — {detail}",
                    detail="This is this channel's own overwrite, not the server's role.",
                )
            )
    return found


def lockdown_findings(guild, channels, held, *, orphans=None) -> tuple[list, list, list]:
    """What the lockdown ledger looks like from outside, and which locks need repair.

    ``held`` is the ledger read once by the caller — ``{channel_id: scope}``, empty
    when it could not be read, which the caller counts as unread rather than as
    silence. Returns ``(findings, beaten, loose)``. ``beaten`` is a channel whose
    deny is being overruled by an allow and ``loose`` one whose deny is gone
    entirely; both are repair lists for a `fix`, and both are only ever filled with
    channels this server's own ledger says are locked: a lock's intent is written
    down, so *this room is meant to be shut* is a fact rather than an assumption.

    A channel holding the shape of one of our locks with **no row** is the other
    direction — a lock the sweep walks past. ``orphans`` is the list the adoption
    pass itself will consider (``adoption_candidates``, asked by :func:`audit` and
    asked here when it is not passed), and it is what keeps this report honest: a
    finding may only promise the `fix` the pass can actually make. A voice channel
    is not a channel a lockdown holds, and a channel I cannot manage is not one I
    can adopt, so neither is reported under a sentence telling an administrator to
    run `fix` — which is a dead end they can reach twice.
    """
    if orphans is None:
        orphans = server_control.adoption_candidates(guild, set(held))
    adoptable = {int(getattr(channel, "id", 0) or 0) for channel in orphans}
    found: list = []
    beaten: list = []
    loose: list = []
    bot_id = _bot_id(guild)
    for channel_id, _scope in sorted(held.items()):
        channel = guild.get_channel(channel_id)
        if channel is None:
            found.append(
                Finding(
                    level=WARNING,
                    where=f"channel {channel_id}",
                    title="is in the lockdown ledger and Discord does not send it to me",
                    detail=(
                        "Either it was deleted or my role can no longer see it. A "
                        "deleted channel does no harm; one I cannot see is a lock "
                        "nothing enforces, because the sweep cannot read its "
                        "messages either."
                    ),
                )
            )
            continue
        # A hide denies View Channel as well, so its leaks are asked about with the
        # wider set: a lock that was asked to make the room private is beaten by
        # anybody who can still see it, not only by anybody who can type.
        names = server_control.lock_names(hide=claims_private(channel))
        leaks = _leaks(channel, names, bot_id)
        if leaks:
            beaten.append(channel)
            found.append(
                Finding(
                    level=CRITICAL,
                    where=f"#{getattr(channel, 'name', '?')}",
                    title=f"is locked and {_whose(leaks)} can still get in",
                    detail=(
                        "An allow on that overwrite is applied after `@everyone`'s and "
                        "beats the deny — which is what the lock was written without. "
                        "`/check fix:True` denies it, and `/unlock` still restores "
                        "exactly what was here."
                    ),
                )
            )
            continue
        if not _denies(channel, names):
            loose.append(channel)
            found.append(
                Finding(
                    level=WARNING,
                    where=f"#{getattr(channel, 'name', '?')}",
                    title="is in the ledger but its deny is gone",
                    detail=(
                        "Somebody opened it by hand while the row stayed: the sweep is "
                        "all that holds it now, so anything posted there is removed "
                        "while people can type. `/check fix:True` writes the deny "
                        "again; if the opening was meant, `/unlock` forgets the row "
                        "and the room stays open."
                    ),
                )
            )
    for channel in channels:
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id in held:
            continue
        if not server_control.looks_like_our_lock(server_control.everyone_overwrite(channel)):
            continue
        where = f"#{getattr(channel, 'name', '?')}"
        if channel_id in adoptable:
            found.append(
                Finding(
                    level=WARNING,
                    where=where,
                    title="looks locked and is not enforced",
                    detail=(
                        "It carries the overwrite one of my old lockdowns left and there is "
                        "no row for it, so nothing removes what is posted. `/check fix:True` "
                        "adopts the ones Discord's audit log proves are mine."
                    ),
                )
            )
            continue
        if not server_control.lock_applies(channel):
            # The old server-wide lockdown wrote its overwrite on everything it could,
            # voice channels included, so this shape is evidence of one rather than a
            # lock: a lockdown is about text, and this is not a channel one holds.
            found.append(
                Finding(
                    level=NOTE,
                    where=where,
                    title=(
                        "carries the overwrite of an old lockdown and is not a kind of "
                        "channel one holds"
                    ),
                    detail=(
                        "A lockdown is about text, so the old command left its mark here "
                        "without ever being able to hold this room — which is why "
                        "`/check fix:True` leaves it alone. If it should hold here too, "
                        "`/lockdown` in it is the explicit way to make it mine, and "
                        "`/unlock` opens it again."
                    ),
                )
            )
            continue
        found.append(
            Finding(
                level=WARNING,
                where=where,
                title=(
                    "looks locked, is not enforced, and I cannot adopt it — I need "
                    "**Manage Channels** there"
                ),
                detail=(
                    "It carries the overwrite one of my old lockdowns left and there is "
                    "no row for it, so nothing removes what is posted. Adopting one is a "
                    "permission change of my own, and Discord does not allow me one here, "
                    "so `/check fix:True` can only adopt the channels I can manage. "
                    "Grant me **Manage Channels** there and run it again."
                ),
            )
        )
    return found, beaten, loose


def bot_standing(guild) -> list:
    """What this bot can and cannot do here, in the symptom rather than the bit."""
    found = []
    me = getattr(guild, "me", None)
    granted = getattr(me, "guild_permissions", None)
    if granted is None:
        return found
    for name, breaks in BOT_NEEDS:
        if bool(getattr(granted, name, False)):
            continue
        found.append(
            Finding(
                level=WARNING,
                where="my own permissions",
                title=f"I do not have **{name.replace('_', ' ')}** — {breaks}",
                detail="Grant it to my role, or move my role up in **Server Settings → Roles**.",
            )
        )
    top = getattr(me, "top_role", None)
    if top is not None:
        above = [
            role
            for role in list(getattr(guild, "roles", None) or [])
            if getattr(role, "position", 0) >= getattr(top, "position", 0)
            and bool(getattr(getattr(role, "permissions", None), "manage_roles", False))
            and role is not top
            and str(getattr(role, "name", "")) != "@everyone"
        ]
        if above:
            found.append(
                Finding(
                    level=NOTE,
                    where="my own role",
                    title=f"sits below {_whose(above)}",
                    detail=(
                        "Discord refuses a role change or a timeout on anybody whose "
                        "highest role is above mine, so those members cannot be acted on "
                        "until my role is moved up."
                    ),
                )
            )
    return found


async def settings_findings(guild, *, bot=None) -> list:
    """The settings and switches a moderation bot depends on, read back."""
    found = []
    level = str(getattr(getattr(guild, "verification_level", None), "name", "") or "")
    if level in ("none", ""):
        found.append(
            Finding(
                level=NOTE,
                where="the server",
                title="verification level is **none**",
                detail=(
                    "Any account, including one made a minute ago, can join and post "
                    "immediately. **Server Settings → Safety Setup** can require a "
                    "verified email or a phone."
                ),
            )
        )
    filter_name = str(getattr(getattr(guild, "explicit_content_filter", None), "name", "") or "")
    if filter_name in ("disabled", ""):
        found.append(
            Finding(
                level=NOTE,
                where="the server",
                title="the explicit content filter is **off**",
                detail="Nobody is scanning uploaded media. It is one switch in **Safety Setup**.",
            )
        )
    if not _mfa_required(guild):
        found.append(
            Finding(
                level=NOTE,
                where="the server",
                title="moderators do not need **2FA**",
                detail=(
                    "A stolen moderator account can ban and delete without a second "
                    "factor. One switch in **Safety Setup**, and it matters most on a "
                    "server with staff who hold Administrator."
                ),
            )
        )
    raw = None
    channel = None
    try:
        raw = await db.get_setting(int(getattr(guild, "id", 0) or 0), modlog.SETTING)
        if raw:
            channel = guild.get_channel(int(raw))
    except Exception as exc:  # noqa: BLE001 — a setting that cannot be read is named
        print(f"[CHECK] could not read the log channel setting: {exc}", flush=True)
    if not raw:
        found.append(
            Finding(
                level=WARNING,
                where="the log channel",
                title="is not set",
                detail=(
                    "Every case this bot opens — a warning, a timeout, a lockdown — has "
                    "nowhere to be written down, so the record an appeal is read against "
                    "does not exist. `/modlog set` picks one."
                ),
            )
        )
    elif channel is None:
        found.append(
            Finding(
                level=WARNING,
                where="the log channel",
                title=f"is set to a channel that is gone (`{raw}`)",
                detail="`/modlog set` picks a new one; until then cases are only in the database.",
            )
        )
    else:
        allowed = modlog.can_post(channel, getattr(guild, "me", None))
        if allowed is False:
            found.append(
                Finding(
                    level=WARNING,
                    where=f"#{getattr(channel, 'name', '?')}",
                    title="is the log channel and I cannot post in it",
                    detail=(
                        "Cases are being written to the database and nowhere you can "
                        "read them; give my role **Send Messages** and **Embed Links** "
                        "there."
                    ),
                )
            )
    enabled = getattr(bot, "is_module_enabled", None)
    if callable(enabled):
        try:
            if not enabled(int(getattr(guild, "id", 0) or 0), "automod"):
                found.append(
                    Finding(
                        level=WARNING,
                        where="the Automod system",
                        title="is switched off",
                        detail=(
                            "Invites, links and floods are not being filtered at all. An "
                            "administrator can turn it on in the dashboard, or the "
                            "Auto Moderation card on the Systems page."
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — a switch that cannot be read is named
            print(f"[CHECK] could not read the automod switch: {exc}", flush=True)
    if not bool(getattr(getattr(bot, "intents", None), "members", False)):
        found.append(
            Finding(
                level=NOTE,
                where="the default role",
                title="cannot be handed out while the **Members intent** is off",
                detail=(
                    "Discord never tells the bot that somebody joined, so the setting "
                    "looks configured and silently does nothing."
                ),
            )
        )
    elif not await _has_default_role(guild):
        found.append(
            Finding(
                level=NOTE,
                where="the default role",
                title="is not set",
                detail="New members arrive with no role at all; `/defaultrole set` gives them one.",
            )
        )
    return found


def _mfa_required(guild) -> bool:
    """Whether this server makes its moderators use 2FA, from either shape.

    discord.py 2.4+ hands back an enum **value** (``MFALevel.require_2fa``), and
    ``.value`` is the only honest read of it: it has **no ``__int__``**, so
    ``int()`` raises — which is what answered every real `/check` with *an
    unexpected error occurred*, while the laws passed because their fake was the
    plain int ``1`` — and it is **truthy even for ``disabled``**, so ``bool()``
    is wrong too, and ``disabled == 1`` is ``False``, so comparing to the bit is
    wrong as well. Older discord.py hands back the int itself, and both are read
    here. A name this module has never seen is read as *not required*, which is
    the loud direction: the card tells the server about a switch rather than
    silently assuming it is on.
    """
    level = getattr(guild, "mfa_level", 0)
    value = getattr(level, "value", None)
    if isinstance(value, int):
        return bool(value)
    if isinstance(level, int):
        return bool(level)
    return str(getattr(level, "name", level) or "").lower() in ("require_2fa", "elevated")


async def _has_default_role(guild) -> bool:
    try:
        return bool(await db.get_setting(int(getattr(guild, "id", 0) or 0), "default_role_id"))
    except Exception:  # noqa: BLE001 — unreadable answers nothing, and says nothing
        return True


async def _ledger(guild) -> tuple[dict, int]:
    """The lockdown ledger, and whether it could be read (``1`` when it could not).

    The read is the one fact in this scan that comes from the database, and it is
    asked of the module that owns it. A failure is counted rather than swallowed:
    *nothing found* answered by a scan that could not open its own records is the
    failure this module's docstring is written against.
    """
    try:
        return await server_control.locked_channels(int(getattr(guild, "id", 0) or 0)), 0
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is named, not guessed
        print(f"[CHECK] could not read the lockdown ledger: {exc}", flush=True)
        return {}, 1


async def audit(guild, *, bot=None) -> Audit:
    """Read one server and answer with every hole, worst first."""
    report = Audit(guild_id=int(getattr(guild, "id", 0) or 0))
    channels = channels_of(guild)
    report.checked = len(channels)
    held, unread = await _ledger(guild)
    report.unread += unread
    report.findings.extend(visibility_notes(guild, channels, locked=set(held)))
    report.findings.extend(hidden_rooms(channels, guild))
    report.findings.extend(stray_marks(guild, channels))
    # The rooms a `fix` may put back under their category, from the same predicate the
    # pass itself asks: the old unlock's exact mark, where removing it takes nothing
    # away from `@everyone`.
    report.strays = [
        channel for channel in channels if server_control.stray_unlock_is_undoable(guild, channel)
    ]
    report.findings.extend(everyone_power(guild))
    report.findings.extend(channel_power(guild, channels))
    # Asked once and handed to the findings rather than asked twice: what a `fix` may
    # claim is this list, and the report's own sentence about refusing them is built
    # from the same answer.
    orphans = server_control.adoption_candidates(guild, set(held))
    found, beaten, loose = lockdown_findings(guild, channels, held, orphans=orphans)
    report.findings.extend(found)
    report.beaten = beaten
    report.loose = loose
    report.orphans = orphans
    report.findings.extend(bot_standing(guild))
    report.findings.extend(await settings_findings(guild, bot=bot))
    report.findings.sort(key=lambda finding: LEVELS.index(finding.level))
    return report
