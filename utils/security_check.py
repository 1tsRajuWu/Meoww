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


def lockdown_findings(guild, channels, held) -> tuple[list, list, list]:
    """What the lockdown ledger looks like from outside, and which locks need repair.

    ``held`` is the ledger read once by the caller — ``{channel_id: scope}``, empty
    when it could not be read, which the caller counts as unread rather than as
    silence. Returns ``(findings, beaten, loose)``. ``beaten`` is a channel whose
    deny is being overruled by an allow and ``loose`` one whose deny is gone
    entirely; both are repair lists for a `fix`, and both are only ever filled with
    channels this server's own ledger says are locked: a lock's intent is written
    down, so *this room is meant to be shut* is a fact rather than an assumption.

    A channel holding the shape of one of our locks with **no row** is the other
    direction — a lock the sweep walks past — and it is reported rather than
    repaired here, because only the audit log can say whether it was ours.
    """
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
        if int(getattr(channel, "id", 0) or 0) in held:
            continue
        if not server_control.looks_like_our_lock(server_control.everyone_overwrite(channel)):
            continue
        found.append(
            Finding(
                level=WARNING,
                where=f"#{getattr(channel, 'name', '?')}",
                title="looks locked and is not enforced",
                detail=(
                    "It carries the overwrite one of my old lockdowns left and there is "
                    "no row for it, so nothing removes what is posted. `/check fix:True` "
                    "adopts the ones Discord's audit log proves are mine."
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
    if not int(getattr(guild, "mfa_level", 0) or 0):
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
    report.findings.extend(everyone_power(guild))
    report.findings.extend(channel_power(guild, channels))
    found, beaten, loose = lockdown_findings(guild, channels, held)
    report.findings.extend(found)
    report.beaten = beaten
    report.loose = loose
    report.findings.extend(bot_standing(guild))
    report.findings.extend(await settings_findings(guild, bot=bot))
    report.findings.sort(key=lambda finding: LEVELS.index(finding.level))
    return report
