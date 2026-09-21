"""Who is asking, as one object rather than a string compared eleven times.

Every authorization decision in ``keep_alive`` was a comparison against
``session["auth_scope"]``: three literals (``"global"``, ``"user"``, ``"guild"``),
written in five places and read in eleven, with the rules about what each one
*means* living in the comments beside whichever comparison needed them. The
failure mode is not one wrong comparison — it is that the meaning of a scope has
no home, so the twelfth reader of ``auth_scope`` inherits whatever it can guess,
and a scope that gains a second condition (as ``"user"`` did, when it stopped
being allowed to edit identity in servers it does not administer) has to be
changed everywhere it is read.

So the session's answer is assembled once, here, and every caller asks the
object:

* :meth:`Principal.may_act_in` — may this session act on this server at all? One
  method, because every question this project asks about a server turns out to be
  this one: the identity panel's "may I rename the bot here" and the toggles' "may
  I switch this off here" are asked about the same set, and when they were two
  functions they were two copies of the same three branches. The one that *looks*
  narrower — renaming the bot in a server somebody administers but did not open —
  is the same answer, and the sentence that used to explain it here now lives in
  ``keep_alive._may_edit_identity``, where the worry was.
* :attr:`Principal.is_operator` — the passkey's own scope, the whole fleet.

Three scopes, and what each means — the whole table, in one place, for the first
time:

* ``global`` — signed in with the passkey (or a Discord identity a deployment has
  deliberately chosen to trust with the fleet). Every server the bot is in.
* ``user`` — signed in with Discord. Only the servers it administers, and that
  list is recomputed from the bot's live guilds on every request rather than
  trusted from the cookie.
* ``guild`` — a one-time link minted by one server's ``/help`` panel. That
  server, and nothing else, for as long as the bot is still in it.

Nothing here decides whether a session is *valid*; that is
``utils/sessions.py`` and Flask's own cookie. This is only the policy half.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: The operator's scope: the passkey, and the whole fleet.
SCOPE_GLOBAL = "global"
#: A Discord sign-in: the servers that person administers.
SCOPE_USER = "user"
#: A one-time link: the one server it was minted in.
SCOPE_GUILD = "guild"

#: Every scope this deployment can grant, for validation and for prose.
SCOPES = (SCOPE_GLOBAL, SCOPE_USER, SCOPE_GUILD)

#: What each scope is called where a person reads it.
LABELS = {
    SCOPE_GLOBAL: "operator (whole fleet)",
    SCOPE_USER: "Discord sign-in",
    SCOPE_GUILD: "one-time link (one server)",
    "": "unknown",
}


@dataclass(frozen=True)
class Principal:
    """One session, as the rules see it."""

    scope: str = ""
    user_id: int | None = None
    guild_id: int | None = None
    #: Servers a Discord sign-in administers. Ids only; see ``_visible_guilds``.
    administered: tuple[int, ...] = field(default_factory=tuple)
    #: The signed-in person's display name, when there is one.
    name: str = ""
    #: This Discord account *is* the owner's — a claim about identity, which buys
    #: the copy on the sign-in page and never the console.
    owner_candidate: bool = False

    @property
    def is_operator(self) -> bool:
        """Whether this session may touch the whole fleet."""
        return self.scope == SCOPE_GLOBAL

    @property
    def is_administered_session(self) -> bool:
        """A Discord sign-in, whose reach is the servers it administers."""
        return self.scope == SCOPE_USER

    @property
    def is_scoped_link(self) -> bool:
        """A one-time link, pinned to one server."""
        return self.scope == SCOPE_GUILD

    @property
    def signed_in(self) -> bool:
        """Whether the session carries a scope at all."""
        return self.scope in SCOPES

    @property
    def may_switch_guild(self) -> bool:
        """Whether the server picker is worth offering this session.

        A one-time link is pinned to one server, so there is nothing to pick;
        the operator and a Discord sign-in both reach more than one.
        """
        return self.scope in (SCOPE_GLOBAL, SCOPE_USER)

    @property
    def label(self) -> str:
        return LABELS.get(self.scope, self.scope or "unknown")

    def may_act_in(self, guild_id, administered=None) -> bool:
        """Whether this session may act on one server.

        ``administered`` overrides the list for a caller that has already
        recomputed it (``_visible_guilds`` does, every request); the default is
        the list as the session carries it.
        """
        gid = _as_int(guild_id)
        if gid is None:
            return False
        if self.is_operator:
            return True
        if self.is_scoped_link:
            return self.guild_id == gid
        if self.is_administered_session:
            allowed = self.administered if administered is None else tuple(administered)
            return gid in allowed
        return False

    def describe(self) -> dict:
        """The shape the console's Access page lists."""
        return {
            "scope": self.scope,
            "label": self.label,
            "user_id": str(self.user_id) if self.user_id else "",
            "guild_id": str(self.guild_id) if self.guild_id else "",
            "name": self.name,
        }


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def from_session(session) -> Principal:
    """This request's session, as a :class:`Principal`.

    Reads the session and nothing else: it does not verify that the sign-in is
    still believed (``keep_alive.is_authenticated`` asks
    ``utils/sessions.py``), and it does not consult the bot's live guild list, so
    a caller that needs either passes the result of its own check alongside.
    """
    raw = session.get("admin_guild_ids") or []
    administered: list[int] = []
    for entry in raw:
        gid = _as_int(entry)
        if gid is not None:
            administered.append(gid)
    return Principal(
        scope=str(session.get("auth_scope") or ""),
        user_id=_as_int(session.get("user_id")),
        guild_id=_as_int(session.get("guild_id")),
        administered=tuple(administered),
        name=str((session.get("discord_user") or {}).get("name") or ""),
        owner_candidate=bool(session.get("owner_candidate")),
    )
