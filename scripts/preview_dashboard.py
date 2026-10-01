"""Serve the real dashboard and market templates with stub APIs, to eyeball them.

Usage (run from the repo root): ``python scripts/preview_dashboard.py``
then open http://127.0.0.1:8099/ (or the port it prints — ``PORT`` moves it).

Verification harness only: it renders ``templates/dashboard.html`` and
``templates/market_live.html`` exactly as their routes do, and answers the
endpoints those pages call with plausible data so their own JavaScript runs end
to end. Nothing here is imported by the bot.

It writes to a **copy** of the database, in a temporary directory — not to the
project's ``bot.db``. The pages here toggle real modules and queue real
scheduled messages, so a preview that shares the live database is a preview that
edits production state.

It has no session, and says so: ``/login`` and ``/logout`` (the header's own
Sign out button) answer a page explaining that instead of Flask's 404.

Where a payload has a builder in the real code — the market's quotes and
candles, the module catalog, the scheduling limits — this file calls it rather
than describing it, because a stub that is *written* to look right is a stub
that drifts, and the page then gets blamed for the harness's shape.
"""
import asyncio
import datetime
import html
import os
import re
import shutil
import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

# Used where a stand-in needs an attribute and nothing else (the support
# channel's guild, and the bot's own member in it): the shapes Discord hands over
# that no reader in this process inspects beyond one field.
from types import SimpleNamespace

from flask import Flask, jsonify, redirect, render_template, request, session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# This harness gets its own copy of the database
#
# The pages it serves write through the *real* ``utils.db`` — module toggles,
# scheduled messages, channel bindings — so driving this preview used to change
# the operator's live state. It did: a click on the bulk toggle during a
# playthrough turned every module off in the Development server, on the database
# the running bot then uploaded as the production backup, and the symptom was
# commands answering "the X system is currently turned off" in the only server
# the operator was watching.
#
# A copy, not a promise: what a person clicks here is discarded with the process,
# and the live file is never opened. It is made *before* ``keep_alive`` is
# imported, because that import is what reads the path.
_LIVE_DB = os.path.join(ROOT, "bot.db")
_WORKDIR = tempfile.mkdtemp(prefix="meow-preview-")
_PREVIEW_DB = os.path.join(_WORKDIR, "bot.db")
_COPIED_LIVE_DB = os.path.exists(_LIVE_DB)
if _COPIED_LIVE_DB:
    shutil.copy2(_LIVE_DB, _PREVIEW_DB)

from utils import db as _db  # noqa: E402

_db.DB_PATH = _PREVIEW_DB

#: Run **whether or not** a live database was copied, and the second half of that
#: was learned by running this file rather than by reading it. A copied file
#: carries whatever schema the live bot last ran ``setup()`` against, so a harness
#: started after a change that adds a table served a preview of the *old* schema:
#: the operator-override table was missing here, every module read fell back to the
#: servers' own switches, and the only sign of it was one line in this process's
#: log — a preview that looks right and answers a question nobody asked. The bot's
#: own ``setup()`` is ``CREATE TABLE IF NOT EXISTS`` throughout and costs
#: milliseconds, so it runs against the copy either way. (A checkout with no
#: ``bot.db`` was the first half of this: an empty file with no tables answered
#: ``500 no such table: guild_modules`` on every switch.)
#:
#: The schema is the bot's own idempotent ``setup()``, the same call
#: ``tests/__init__.py`` makes for its own file.
def _build_schema() -> None:
    asyncio.run(_db.setup())


try:
    asyncio.get_running_loop()
except RuntimeError:
    _build_schema()
else:
    # Imported from inside a running loop (a test that loads this file from
    # an async case): an import may not block on that loop, and the schema is
    # needed before the first request, so it is built in a thread of its own
    # rather than scheduled and hoped for.
    _schema_thread = threading.Thread(target=_build_schema, daemon=True)
    _schema_thread.start()
    _schema_thread.join()

# --------------------------------------------------------------------------- #
# A preview client id, so the two Discord surfaces can be looked at
#
# The sign-in button and the "add to a server" link are both gated on a client
# id, which a preview process does not have — without one they are invisible in
# the one tool that exists to look at the page.
#
# The id itself is **public by design** and is already committed: it is in every
# invite link anybody can read, and ``render.yaml`` stores it as a value rather
# than a prompt for exactly that reason. So the preview reads it from there and
# its "Add to Discord" button goes to the real application. The placeholder that
# used to stand in here made that button a dead end — Discord answers
# "application not found" — which is the wrong thing for a preview to teach about
# the page's most important link. Only the client *secret* is made up: nothing in
# this file talks to Discord, which is why ``/login/discord`` says so instead of
# redirecting.
_PLACEHOLDER_CLIENT_ID = "1234567890123456789"


def _committed_client_id() -> str:
    """The public application id the deploy manifest commits, if it is readable."""
    try:
        manifest = (Path(__file__).resolve().parent.parent / "render.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(
        r'^\s*-\s*key:\s*DISCORD_CLIENT_ID\s*\n\s*value:\s*"?(\d[\d]*)"?',
        manifest,
        flags=re.MULTILINE,
    )
    return match.group(1) if match else ""


def prepare_environment() -> None:
    """The two settings a *served* preview needs, and nothing else's business.

    Called by the entry point below and **never on import**, which is a
    correction: this file is imported by the test suite for its renderers (and by
    two laws that load a copy under a name of their own), and a module that
    reconfigures the process's environment as a side effect of being imported
    reconfigures every test that runs after it. The damage is quiet and it landed
    on a header: `os.environ.setdefault("DASHBOARD_BASE_URL", …)` left this
    machine's own address in the environment, and
    `keep_alive.dashboard_base_url()` reads an explicit setting *before* the
    request's own forwarded host — so from the first import onward, a page rendered
    for `dacatdashboard.pages.dev` was told it lived at `http://localhost:0`,
    which is what one of the header laws caught the day it was written.
    """
    if not os.environ.get("DISCORD_CLIENT_ID"):
        real = _committed_client_id()
        os.environ["DISCORD_CLIENT_ID"] = real or _PLACEHOLDER_CLIENT_ID
        os.environ.setdefault("DISCORD_CLIENT_SECRET", "preview-only-no-real-secret")
        print(
            "[PREVIEW] No DISCORD_CLIENT_ID in the environment — using "
            + (f"the public id from render.yaml ({real}) so the invite link works."
               if real else
               "a placeholder id so the Discord buttons render; that invite link "
               "will not resolve.")
            + " No request is ever made to Discord.",
            flush=True,
        )
    os.environ.setdefault(
        "DASHBOARD_BASE_URL", f"http://localhost:{os.environ.get('PORT', '8099')}"
    )

app = Flask(
    __name__,
    template_folder=os.path.join(ROOT, "templates"),
    # The shared stylesheet lives at the project root, and this script runs from
    # ``scripts/``, so Flask's default ``static`` folder would be
    # ``scripts/static`` — which does not exist, and every page here came out
    # unstyled: the harness was the one place the design system could not be
    # looked at, which is the only thing this file is for.
    static_folder=os.path.join(ROOT, "static"),
)

# Templates are edited *while this harness runs* — that is the whole point of it.
# Flask defaults ``TEMPLATES_AUTO_RELOAD`` to ``app.debug``, which is off here, so a
# long-lived preview would keep serving the bytecode-cached template from before the
# edit: you change the page, reload, and look at the old one. Cheaper to reread the
# file per request than to trust the cache, and the harness never serves real users.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# A secret key, because the real sign-in view stores a CSRF token in the
# session and Flask refuses to write one without it. This is a preview only:
# it never leaves the process, and nothing this harness holds is a credential.
app.secret_key = "preview-only-not-a-credential"

# The real app is imported for two reasons now: its security headers, applied to
# every response below (otherwise this harness would prove nothing about whether
# the pages survive our own CSP), and the cache-busting token its templates ask
# for. This app is not `keep_alive`'s and Jinja 3 keeps each environment's globals
# to itself, so the harness has to be handed the same function — otherwise every
# page here renders without the token its stylesheet link needs, which is exactly
# the sort of thing only the harness would show.
import keep_alive  # noqa: E402

app.jinja_env.globals["asset_version"] = keep_alive.asset_version
app.jinja_env.globals["brand"] = keep_alive.brand

from utils import (  # noqa: E402
    command_publish, command_scope, digest, discord_auth, features, identity,
    market as market_module, sessions, uptime_status,
)


@app.after_request
def _old_policy_for_comparison(response):
    """``?csp=old`` reproduces the header that blocked the chart library.

    Registered *before* the real hook: Flask runs after-request handlers in
    reverse registration order, so this one runs last and wins.
    """
    if request.args.get("csp") == "old":
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net;",
            "'self' 'unsafe-inline';",
        )
    return response


app.after_request(keep_alive.set_security_headers)

#: What the page last asked a toggle endpoint for. Same idea as ``LAST_SEND``:
#: it exists so a check can ask the page's question and read the answer back.
LAST_TOGGLE: dict = {}

#: The command names the previewed page says this server is registered with.
#: Taken from the registry — the same source the publisher reads — so the panel's
#: counts and pills are the ones the real rule would produce for this module
#: state. Invented names here would make a correct panel look wrong.
#:
#: ``MODULE_COMMAND_MAP`` and not the features' ``gate_commands``: the commands
#: several modules gate together (the casino pair, ``/sellanimals``, ``/flex``)
#: live in ``SHARED_GATES`` and appear in no feature's own list, so reading the
#: latter left the preview claiming the bot had 79 commands where it has 90 —
#: a harness that under-counts makes a correct panel look broken.
PREVIEW_COMMANDS = sorted(
    set(features.MODULE_COMMAND_MAP) | set(features.ALWAYS_ALLOWED_COMMANDS)
)


def record_healthy_publish(guild_id):
    """What the publisher would have written down on a healthy deployment.

    Written through the real ledger rather than into the template, so the line
    is composed by the module that owns the words — which is the point: the
    panel is painted twice, by the view and by the page's own refresh, and a
    harness that seeded its own sentence would hide exactly the drift this file
    exists to catch. The figure comes from the same rule the real view uses, so
    it cannot disagree with the counts beside it.

    A preview of a *working* deployment is a stub, deliberately, the way
    ``bot_ready=True`` is: what it must not do is invent wording.
    """
    states = {module["id"]: module["enabled"] for module in MODULES}
    summary = command_scope.summarise(PREVIEW_COMMANDS, states)
    command_publish.record(
        guild_id,
        verified=True,
        pending=0,
        held=summary["available"],
        detail="",
        retry_after=None,
    )
    return command_publish.describe(guild_id, expected=summary["available"])

#: The cards come from the registry, not a hand-written list. The list below
#: was all three, and it stayed behind: it still offered **Music**, which the
#: registry retired, so the preview showed a switch that could never work and
#: answered it with "Could not save that change. Please try again." — a fake
#: *transient* error for a module that no longer exists, on the one tool whose
#: job is showing what the page really does. ``dashboard_modules()`` is the same
#: function the real view renders, so the preview can no longer disagree with
#: production about which systems exist.
MODULES = features.dashboard_modules()
for _module in MODULES:
    _module["enabled"] = features.default_enabled(_module["id"])

#: Real-sized ids, as strings, on purpose. A snowflake this big (bigger than
#: 2**53) is the only kind that can show a browser rounding it, and that is what
#: "every send answers *that channel no longer exists*" actually was: stub ids of
#: 1 and 2 survive any arithmetic, so this harness used to prove nothing about the
#: one number the page has to hand back untouched.
CHANNELS = [
    {"id": "1548437081120833657", "name": "general", "category": "Community", "kind": "text", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833658", "name": "announcements", "category": "Community", "kind": "text", "can_send": True, "slowmode": 0, "is_news": True},
    {"id": "1548437081120833659", "name": "memes", "category": "Community", "kind": "text", "can_send": True, "slowmode": 30, "is_news": False},
    {"id": "1548437081120833660", "name": "staff-chat", "category": "Staff", "kind": "text", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833661", "name": "mod-logs", "category": "Staff", "kind": "text", "can_send": False, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833662", "name": "welcome", "category": "No category", "kind": "text", "can_send": False, "slowmode": 0, "is_news": False},
    # Voice channels, because a voice channel has a text chat of its own and a
    # message can land in it — the picker used to leave them out entirely.
    {"id": "1548437081120833663", "name": "General Voice", "category": "Voice", "kind": "voice", "can_send": True, "slowmode": 0, "is_news": False},
    {"id": "1548437081120833664", "name": "Music", "category": "Voice", "kind": "voice", "can_send": False, "slowmode": 0, "is_news": False},
]

MEMBERS = [
    {"id": "1542296506290143349", "label": "RAJ", "sub": "@raj", "kind": "user"},
    {"id": "1542296506290143350", "label": "DaCat", "sub": "@dacat", "kind": "user"},
    {"id": "1542296506290143351", "label": "Bob", "sub": "@bob", "kind": "user"},
    {"id": "1542296506290143352", "label": "Bobette", "sub": "@bobette", "kind": "user"},
]

ROLES = [
    {"id": "1546716293657530368", "label": "Members", "kind": "role"},
    {"id": "1546716293657530369", "label": "Moderator", "kind": "role"},
]

SPECIAL = [
    {"id": "@everyone", "label": "@everyone", "sub": "everyone in the server", "kind": "special"},
    {"id": "@here", "label": "@here", "sub": "everyone online right now", "kind": "special"},
]


#: The servers this harness pretends the bot is in. The page's own switcher
#: navigates to ``/dashboard?guild_id=``, so the preview has to answer for the
#: server that was asked for: it used to render the first one whatever the query
#: said, which made the switcher look broken in the one place it gets checked.
GUILDS = [
    {"id": 777001, "name": "DaCat Server", "members": 1627},
    {"id": 777002, "name": "Second Server", "members": 42},
]

class _PreviewMentionable:
    """A member or role carrying only what ``_build_message`` reads off one.

    The real builder needs ``.id`` and ``.mention``; a stub shaped like the
    endpoint's own JSON would raise an ``AttributeError`` halfway through it.
    """

    def __init__(self, entry: dict, prefix: str):
        self.id = int(entry["id"])
        self.mention = f"<{prefix}{entry['id']}>"
        self.name = str(entry.get("label") or "")


class _PreviewGuild:
    """The guild half of ``keep_alive._build_message``, over this file's stubs.

    The harness has no gateway, so there is nothing real to resolve a tag
    against — but every *refusal* the builder makes (which channels, how many,
    how long, how many tags, a title with no embed) is decided before it looks
    at a member, and the sentence the preview shows should be the bot's own.
    Answering ``None`` for an id it was not given is what the real code calls
    *unresolved*, so an id the preview never offered comes back as ignored
    rather than silently accepted.
    """

    def __init__(self):
        self.id = GUILDS[0]["id"]
        self._members = {int(m["id"]): _PreviewMentionable(m, "@") for m in MEMBERS}
        self._roles = {int(r["id"]): _PreviewMentionable(r, "@&") for r in ROLES}

    def get_member(self, user_id):
        try:
            return self._members.get(int(user_id))
        except (TypeError, ValueError):
            return None

    def get_role(self, role_id):
        try:
            return self._roles.get(int(role_id))
        except (TypeError, ValueError):
            return None

    async def fetch_member(self, user_id):
        """No gateway, so nothing to ask: the real code treats this as ignored."""
        return None


class _PreviewCommand:
    """One entry of ``tree.get_commands()``, as ``_command_tree_names`` reads it."""

    def __init__(self, name: str):
        self.name = name


class _PreviewTree:
    """The bot's command tree, seeded from the registry.

    The console's Commands page compares the registry against what Discord was
    *told*, and without a tree it cannot tell those apart — every declared
    command would read as "not published". The names come from
    ``PREVIEW_COMMANDS``, which is already derived from the registry, so the two
    sides of that comparison are the real ones for this module state.
    """

    def get_commands(self):
        return [_PreviewCommand(name) for name in PREVIEW_COMMANDS]


class _PreviewKind:
    """The ``.name`` half of an enum, for the two readers that want one.

    A channel's kind and a member's presence both arrive as ``discord`` enums in
    production and both readers take the enum's own ``name`` — so a stub with
    ``type.name`` and ``status.name`` is read by the real code rather than by a
    preview-shaped branch of it.
    """

    def __init__(self, name: str):
        self.name = name


class _PreviewChannel:
    """One channel, as the Live page's reader looks at one."""

    def __init__(self, cid, name, kind, *, category=None, position=0, topic="", limit=None):
        self.id = cid
        self.name = name
        self.type = _PreviewKind(kind)
        self.category = _PreviewCategory(category) if category else None
        self.position = position
        self.topic = topic
        self.nsfw = False
        self.slowmode_delay = 0
        self.user_limit = limit or 0
        # The fallback source, for a reader that has no voice states to hand.
        self.members = []


class _PreviewSupport:
    """The support channel, as the bridge's own two writes read one.

    The card this stands behind is the one surface here that is **two-way**: a
    message typed into the preview is posted through ``keep_alive``'s own route
    and written into the harness's copy of the store, and the transcript under
    the form is read back out of it — so the piece worth looking at (does a
    message I send appear where I expect it, and do the permissions say what the
    card claims) can actually be driven rather than described. ``send`` returns a
    real-shaped message, because the route records Discord's id and jump link off
    what it gets back. Empty history, and honestly so: this process has no gateway
    to fetch a channel's real past from, and a pulled message the harness invented
    would be a preview of something that never happened.
    """

    def __init__(self, cid, name="support"):
        self.id = int(cid)
        self.name = name
        self.mention = f"<#{cid}>"
        # The bot's own member *in the channel's server*, which is whose
        # permissions Discord applies — and the answer the card prints.
        self.guild = SimpleNamespace(me=SimpleNamespace(id=999))
        self._sent = 0

    def permissions_for(self, member):
        return SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True
        )

    async def send(self, content=None, **kwargs):
        self._sent += 1
        return SimpleNamespace(
            id=900000 + self._sent,
            jump_url=f"https://discord.com/channels/0/{self.id}/{900000 + self._sent}",
        )

    async def history(self, limit=50):
        """No past to hand over: see the class docstring."""
        for message in ():
            yield message


class _PreviewCategory:
    """A category, *with* its type.

    Without one it reads as a text channel — the Live page classifies by
    Discord's own ``type``, since ``isinstance`` cannot see a stand-in — and the
    preview then draws "# Chat" as a channel with nothing in it, which looks
    exactly like a page that has misread a guild.
    """

    def __init__(self, name: str, cid=None):
        self.name = name
        self.id = cid
        self.type = _PreviewKind("category")


class _PreviewVoiceState:
    """One entry of ``Guild.voice_states``: an id, a channel, and the flags."""

    def __init__(self, user_id, channel, **flags):
        self.user_id = user_id
        self.channel = channel
        self.user = None
        self.mute = flags.get("mute", False)
        self.deaf = flags.get("deaf", False)
        self.self_mute = flags.get("self_mute", False)
        self.self_deaf = flags.get("self_deaf", False)
        self.self_stream = flags.get("self_stream", False)
        self.self_video = flags.get("self_video", False)
        self.suppress = flags.get("suppress", False)


class _PreviewServer:
    """One server as the console's own views read one.

    ``_fleet_rows`` wants an id, a name, a member count, the bot's own member in
    it (for the nickname column), and a member lookup (for the richest list);
    ``/api/console/live`` wants its channels, its voice states and the presence
    of its members. That is exactly what this carries — the shapes the real code
    reads, rather than a hand-built JSON row that would drift from it.

    The channels and voice states are here because the alternative was measured:
    with none of them the Live page renders "0 channels · 0 in voice", which is
    honest and useless — it is **this harness's** shape rather than the page's
    behaviour, and a preview that shows an empty console teaches nothing about
    the console. Same lesson the economy seeding below was added for.
    """

    def __init__(self, entry: dict):
        self.id = int(entry["id"])
        self.name = str(entry["name"])
        self.member_count = int(entry["members"])
        self.icon = None
        # No dedicated bot member: `_identity_member` falls back to the bot's own
        # user, which this harness also does not have, and both paths answer ""
        # rather than raising. A fabricated "DaCat" here would be a nickname the
        # preview invented about a gateway it does not have.
        self.me = None
        self._members = {int(m["id"]): _PreviewMentionable(m, "@") for m in MEMBERS}
        base = self.id * 1000
        self.text_channels = [
            _PreviewChannel(base + 1, "general", "text", category="Chat", topic="Anything goes."),
            _PreviewChannel(base + 2, "media", "text", category="Chat", position=1),
            _PreviewChannel(base + 3, "staff", "text", category="Staff"),
        ]
        self.voice_channels = [
            _PreviewChannel(base + 11, "Lobby", "voice", category="Voice", limit=10),
            _PreviewChannel(base + 12, "Focus Room", "voice", category="Voice", position=1),
            _PreviewChannel(base + 13, "AFK", "voice", category="Voice", position=2),
        ]
        self.categories = [
            _PreviewCategory(name, base + 20 + index)
            for index, name in enumerate(("Chat", "Staff", "Voice"))
        ]
        self.channels = [*self.text_channels, *self.voice_channels, *self.categories]
        lobby, focus = self.voice_channels[0], self.voice_channels[1]
        people = list(self._members.values())
        # Presence, which needs a status on each member — so the Live page's
        # "who is around" card can be looked at rather than only believed.
        for index, member in enumerate(people):
            member.status = _PreviewKind(("online", "idle", "dnd", "offline")[index % 4])
            member.bot = False
            member.activities = []
        self.members = people
        self.voice_states = {
            people[0].id: _PreviewVoiceState(people[0].id, lobby, self_stream=True),
            people[2].id: _PreviewVoiceState(people[2].id, lobby, self_mute=True),
            people[1].id: _PreviewVoiceState(people[1].id, focus, self_video=True),
        }

    def get_member(self, user_id):
        try:
            return self._members.get(int(user_id))
        except (TypeError, ValueError):
            return None


class _PreviewCompany:
    """The voice companion, as ``cogs.company.Company.snapshot`` answers it.

    Shaped from the same dict the real cog returns — sitting, waiting, left_us,
    blocked_seconds, grace and cooldown — rather than from the console's own
    idea of it, so the preview fails in the same way the page would if that shape
    ever changed.
    """

    def __init__(self, guild, channel, person):
        self._guild = guild
        self._channel = channel
        self._person = person

    def snapshot(self) -> dict:
        if self._channel is None or self._person is None:
            return {}
        return {
            "sitting": {
                "guild_id": str(self._guild.id),
                "guild": self._guild.name,
                "channel_id": str(self._channel.id),
                "channel": self._channel.name,
                "person_id": str(self._person.id),
                # The same reading ``keep_alive._live_name`` makes: a display
                # name where the object has one, and the plain name otherwise —
                # these stand-ins carry only the second, exactly as a member
                # without the Server Members intent does.
                "person": getattr(self._person, "display_name", None)
                or getattr(self._person, "name", ""),
                "since": int(time.time()) - 214,
                "streaming": False,
                "video": True,
                "greeted": True,
                "reason": "they were alone in the room",
            },
            "waiting": [],
            "left_us": [],
            "blocked_seconds": 0,
            "grace_seconds": 12.0,
            "cooldown_seconds": 300.0,
        }


class _PreviewBot:
    """The gateway the console's real views read.

    ``/api/console/*`` are ``keep_alive``'s own views, and every one of them
    lists servers through ``BOT_INSTANCE``. This process has no gateway, so with
    nothing assigned they all answered an **empty fleet**: the Fleet page listed
    no servers, the Systems matrix had no columns, and the Commands and Economy
    pages — which are each *about* a server — had nothing to be about. A preview
    of an empty console teaches nothing about the console, which is the whole job
    of this file.

    Assigned under ``__main__`` and not at import, deliberately: the test-suite
    imports this module, and a gateway installed by an import is a gateway every
    test that never asked for one.
    """

    def __init__(self):
        badge = _preview_badge()
        requested = os.environ.get("PREVIEW_GATEWAY", "connected").strip().lower()
        self._has_started = requested != "never_connected"
        self._connected = badge["connected"]
        self.guilds = [_PreviewServer(entry) for entry in GUILDS]
        self.tree = _PreviewTree()
        # `USER_ID`-less on purpose; see `_PreviewServer.me`. `latency` is seconds,
        # which is what `discord.Client` reports and what `_bot_ping_ms` scales.
        self.user = None
        self.latency = int(os.environ.get("PREVIEW_PING", "41")) / 1000
        # The support bridge's one address, so the card on the Overview is a
        # reading rather than a settings row and the form under it can be driven.
        self._support = _PreviewSupport(keep_alive.support.channel_id())

    def get_channel(self, channel_id):
        """The support room, and nothing else.

        ``find``-shaped like ``discord.Client.get_channel`` because that is what
        the bridge calls it as: the support channel is a *deployment* setting, so
        the lookup has to answer for a server other than whichever one a page is
        reading. Everything else comes back ``None``, which the card reports as
        "nothing the bot can see" — the truth for a process with no gateway.
        """
        try:
            wanted = int(channel_id)
        except (TypeError, ValueError):
            return None
        return self._support if wanted == self._support.id else None

    def get_guild(self, guild_id):
        try:
            wanted = int(guild_id)
        except (TypeError, ValueError):
            return None
        return next((guild for guild in self.guilds if guild.id == wanted), None)

    def is_ready(self) -> bool:
        return bool(self._connected)

    def resync_commands(self, guild_id):
        """The gateway's republish hook, as ``bot.resync_commands`` answers it.

        ``keep_alive._resync_commands`` looks for this attribute and reports
        ``republish: false`` when it is missing — the honest answer for a process
        that is still booting, and a lie about this one: without the hook every
        switch in a preview answers *"saved, but the command list was not
        republished — the bot may be restarting"*, so the success toast, which is
        the state this harness exists to make lookable-at, is the one thing it
        could never draw. Answered the way the real hook answers: the gateway has
        that server, so the request can be queued.
        """
        return self.get_guild(guild_id) is not None

    def get_user(self, user_id):
        """The member the Questions page is about to answer, as a DM target.

        ``cogs.qna.deliver_answer`` opens with ``client.get_user(id)`` and falls
        back to ``fetch_user``, then DMs whoever it got — and answers the operator
        *"I couldn't look their account up"* when both come back empty. None of
        that needs a gateway to be true, and without it the one write this page
        has could never be driven in a preview: every reply would report a refusal
        that the deployment it is standing in for would not have made.

        **Synchronous, like Discord's own**: returning ``fetch_user``'s awaitable
        here — which is what the first version of this did — hands
        ``deliver_answer`` a *coroutine* where it expects a member, and the DM
        then fails with ``coroutine.send() takes no keyword arguments``: a
        refusal that looks like Discord's and is entirely the harness's.
        """
        return self._member(user_id)

    async def fetch_user(self, user_id):
        """The same lookup, for the path a cache miss takes."""
        return self._member(user_id)

    def _member(self, user_id):
        """A stand-in member, whose ``send`` records what the bot would have sent."""
        member = next(
            (
                entry
                for guild in self.guilds
                for entry in guild.members
                if str(entry.id) == str(user_id)
            ),
            None,
        )
        return _PreviewUser(member, user_id)

    def get_cog(self, name):
        """The voice companion, as the Live page reads it.

        Only one cog is stood up, and only for the same reason the gateways above
        exist: the companion card's *three* readings (who is alone, who is live,
        where the bot is sitting) are worth looking at, and a preview that showed
        "not sitting with anybody" under a card that explains what it would look
        like is a preview of the empty state only. Nothing here is read by
        anything but ``/api/console/live``.
        """
        if name != "Company":
            return None
        first = self.guilds[0] if self.guilds else None
        if first is None:
            return None
        focus = next(
            (channel for channel in first.voice_channels if channel.name == "Focus Room"),
            None,
        )
        person = first.members[1] if len(first.members) > 1 else None
        return _PreviewCompany(first, focus, person)


class _PreviewUser:
    """The asker, as the DM target ``cogs.qna.deliver_answer`` needs.

    Only ``send`` matters: the delivery builds the card, hands it to ``send``, and
    reads nothing back but the absence of an exception. Keeping what was sent
    means the harness can say what *would* have gone out — which is the only way
    the question "did that reply actually reach somebody" is answerable without
    the answer being invented by the page.
    """

    #: Everything this process has "DMed", newest last. Read by nothing but the
    #: preview itself; the deployment keeps no such list.
    sent: list[dict] = []

    def __init__(self, member, user_id):
        self.id = int(user_id or 0)
        self.name = getattr(member, "name", f"member {int(user_id or 0)}")
        self.display_name = self.name
        self.bot = False

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return None


def seed_activity() -> int:
    """Give the previewed servers a week of counted chatter, and voice time.

    The same reasoning as ``seed_economy`` below, one table over: the tracker's
    rows belong to *real* servers, and the stand-in guilds this harness serves
    appear in none of them — so the Live page's two activity cards rendered a
    table of zeros. That is a true reading of the database and a useless preview
    of a page whose whole job is to show where the talking is.

    Written through the tracker's own columns and its own ``ON CONFLICT`` key, and
    only into a guild that has no rows at all, so re-running the harness neither
    double-counts nor touches a guild somebody is really using.
    """
    conn = _db.connect()
    written = 0
    try:
        for guild in GUILDS:
            existing = conn.execute(
                "SELECT COUNT(*) FROM activity_messages WHERE guild_id = ?", (guild["id"],)
            ).fetchone()[0]
            if existing:
                continue
            base = int(guild["id"]) * 1000
            for offset in range(7):
                day = (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()
                for index, member in enumerate(MEMBERS):
                    conn.execute(
                        "INSERT OR REPLACE INTO activity_messages "
                        "(guild_id, user_id, channel_id, date, count) VALUES (?, ?, ?, ?, ?)",
                        (
                            guild["id"],
                            int(member["id"]),
                            base + 1 + (index % 3),
                            day,
                            max(1, 14 - offset * 2 - index),
                        ),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO activity_voice "
                    "(guild_id, user_id, channel_id, date, seconds) VALUES (?, ?, ?, ?, ?)",
                    (guild["id"], int(MEMBERS[0]["id"]), base + 11, day, 1800 - offset * 120),
                )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def seed_economy() -> int:
    """Give the previewed servers an economy to draw. Returns rows written.

    Only for a guild with no rows at all, so re-running the harness does not
    overwrite balances somebody moved through the console a minute earlier — and
    only for the harness's own stand-in servers, which exist in no real database.
    Written with the real table's own columns, so the page's reader is the thing
    being previewed rather than a second copy of it.
    """
    conn = _db.connect()
    written = 0
    try:
        for guild in GUILDS:
            existing = conn.execute(
                "SELECT COUNT(*) FROM economy WHERE guild_id = ?", (guild["id"],)
            ).fetchone()[0]
            if existing:
                continue
            for index, member in enumerate(MEMBERS):
                conn.execute(
                    "INSERT OR REPLACE INTO economy (user_id, guild_id, wallet, bank) "
                    "VALUES (?, ?, ?, ?)",
                    (int(member["id"]), guild["id"], 12_500 * (index + 1), 3_400 * index),
                )
                written += 1
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meow_bank (guild_id INTEGER PRIMARY KEY, "
                "coins INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meow_bank (guild_id, coins) VALUES (?, ?)",
                (guild["id"], 1_260),
            )
        conn.commit()
    finally:
        conn.close()
    return written


def seed_questions() -> int:
    """Give the Questions page something true to draw. Returns rows written.

    Seeds the real table with the real columns, so what the page reads is the
    deployment's own reader (``qna.owner_inbox_sync``) rather than a copy of it,
    and only when the table is empty — re-running the harness must not resurrect
    a question somebody answered through the console a minute earlier.

    The names are the harness's own stand-in members, and one row is left
    *unrelayed* on purpose: a question the channel never saw is the one state the
    page can show that a hand-written mock never would, and it is the state an
    operator most needs to notice.
    """
    from utils import qna as _qna

    conn = _db.connect()
    written = 0
    try:
        conn.execute(_qna.CREATE_TABLE)
        existing = conn.execute(f"SELECT COUNT(*) FROM {_qna.TABLE}").fetchone()[0]
        if existing:
            return 0
        guild = GUILDS[0]
        for index, (text, relayed, age) in enumerate(
            (
                ("why does the level-up card say cat when the server is set to neutral?", True, 420),
                ("could the starboard threshold be per-channel? half the server is memes", True, 3_900),
                ("are you the same meow that was in the other server? asking for nobody", False, 7_500),
            )
        ):
            member = MEMBERS[index % len(MEMBERS)]
            conn.execute(
                f"INSERT INTO {_qna.TABLE} "
                "(guild_id, guild_name, user_id, user_name, question, asked_at, relayed, "
                " relay_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    # ``label`` rather than ``sub``: the stored name is what the
                    # owner's DM renders, and ``@raj`` is the harness's mention
                    # form rather than a member's name.
                    guild["id"], guild["name"], int(member["id"]), member["label"],
                    text, time.time() - age, 1 if relayed else 0,
                    "" if relayed else "the webhook did not accept it (HTTPError)",
                ),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def seed_sessions() -> int:
    """Give the Access page something true to draw. Returns rows written.

    The ledger is real — this seeds nothing that the page then invents: two
    sign-ins are recorded the way a sign-in records one, and one of them is
    revoked so the list contains each of its three states. Without this the page
    is honest and useless: one row (the harness's own session) and three empty
    windows, which is exactly the state a fresh deployment is in and tells a
    reader nothing about what the page looks like when it has something to say.

    The blocked window is seeded as *attempts* rather than as a verdict, so the
    count, the address and the time left are all computed by the same code the
    deployment runs. Keyed to an address that is documentation-only (RFC 5737),
    so nothing here can name a real visitor.
    """

    written = 0
    # Two rows, as somebody's phone and an old link would have left them. The
    # third — this browser — is the harness's own session, which records itself
    # in `owner_console`: seeding a row for it here would show the operator two
    # of their own sessions, one of which does not exist.
    for label, agent, ip, ended in (
        ("R A J", "Mozilla/5.0 (iPhone) Safari/604", "203.0.113.24", False),
        ("one-time link", "curl/8.7", "198.51.100.9", True),
    ):
        sid = sessions.start("global", label=label, ip=ip, agent=agent)
        if not sid:
            continue
        written += 1
        if ended:
            sessions.revoke(sid)

    # A window that is *partly* used, so the reading has something to show for the
    # fifteen minutes it lasts: three wrong passkeys from one address, which is
    # under the limit and therefore not a lockout. The send window is touched the
    # same way but is one minute wide, so what it demonstrates is that a count
    # ages out — a reading of nothing, correctly, rather than a stale one.
    for _ in range(3):
        keep_alive._PASSKEY.record("198.51.100.44")
    keep_alive._SEND.allow("198.51.100.44")
    keep_alive._SEND.note_refused()
    return written


#: Which server this harness is "in". Production keeps that in the session and a
#: passkey session can move between servers; here it is one process-wide value,
#: which is the only sense in which this harness has a session at all. It exists
#: because the picker's own **Manage** button posts to ``/servers`` and has to go
#: somewhere: answering with the same page left a visible control that did
#: nothing, in the one tool whose job is showing what the page does.
CURRENT_GUILD: dict = {"id": GUILDS[0]["id"]}


#: Where the harness would have written its own answer, it now serves the real
#: view instead. ``keep_alive.home`` answers a monitor with the liveness line and
#: a person with the landing page (or the dashboard, signed in) — the same
#: decisions production makes, made by the same function. A harness that
#: restates a route answers differently from the route, which is where every
#: defect found in this file so far came from.
for _rule, _name, _view in (
    ("/", "home", keep_alive.home),
    ("/privacy", "policy_privacy", keep_alive.policy_privacy),
    ("/terms", "policy_terms", keep_alive.policy_terms),
    ("/login", "login", keep_alive.login),
    # The four the frame's own footer links to, each of which the harness used to
    # answer with Flask's 404: a preview whose every page carries navigation that
    # dead-ends is a preview that cannot show what a reader sees, and the pages
    # behind them are where the product explains itself. `/support` is here for
    # the same reason `/health` is — it is linked from shipped copy, and its
    # answer is a redirect to the donation page rather than a document.
    ("/systems", "systems_page", keep_alive.systems_page),
    ("/start", "start_page", keep_alive.start_page),
    ("/radio", "radio_page", keep_alive.radio_page),
    ("/ai", "policy_ai", keep_alive.policy_ai),
    ("/support", "route_support_redirect", keep_alive.route_support_redirect),
    # The dashboard's own topbar links to Pricing, and the landing page links to
    # it twice; the harness answered both with Flask's 404, so the one control a
    # person is most likely to click from the preview was the one that did not
    # work. `/health` is bound for the smaller reason that it is the endpoint the
    # deployment docs tell an operator to read and UptimeRobot polls — a preview
    # that 404s the address it publishes invites a wrong conclusion about
    # production.
    ("/pricing", "pricing", keep_alive.pricing_page),
    ("/health", "health", keep_alive.health),
):
    app.add_url_rule(_rule, _name, _view, methods=["GET", "POST"])

# The support bridge, which is two views on one address rather than one view on
# two methods: the card's forms post to the write, and a script reads the same
# address for the transcript. Both are ``keep_alive``'s own, for the reason every
# other delegated route here is — and mounting them is what makes the one
# *two-way* card in this harness drivable rather than only lookable-at.
@app.route("/api/support", methods=["GET"])
def preview_api_support():
    """The bridge's read, delegated to the endpoint that owns it.

    The harness signs itself in first for the same reason every other delegated
    write here does: the address is operator-only, and a 401 is not what this file
    exists to show.
    """
    _sign_in()
    return keep_alive.api_support()


@app.route("/api/support", methods=["POST"])
def preview_api_support_write():
    """The bridge's write — the card's own two forms post here."""
    _sign_in()
    return keep_alive.api_support_write()


def _preview_badge() -> dict:
    """Which of the three gateway states this preview should draw.

    `PREVIEW_GATEWAY` names one — ``connected`` (the default, since the rest of
    this harness already renders as if the bot were up), ``reconnecting`` or
    ``never_connected`` — and anything unrecognised falls back to the default
    rather than raising, because a preview that will not start is a worse answer
    than one showing the wrong dot.
    """
    key = os.environ.get("PREVIEW_GATEWAY", "connected").strip().lower()
    if key not in keep_alive._BADGE_STATES:
        key = "connected"
    label, css, pulse = keep_alive._BADGE_STATES[key]
    return {"label": label, "css": css, "pulse": pulse, "connected": key == "connected"}


@app.route("/dashboard")
@app.route("/dashboard/<path:sub>")
def dashboard(sub: str = "overview"):
    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        # No id named: the server this harness is "in", the way production falls
        # back to the session's. It is what makes the picker's **Manage** land on
        # the server that was chosen rather than on the first one in the file.
        guild_id = CURRENT_GUILD["id"]
    # One page of the dashboard, by its own address — the same eleven production
    # serves, so the rail's links are followable here instead of landing on a 404
    # only this harness has. An address that is not a page falls back to the front
    # one, the way ``DASHBOARD_PAGE_BY_PATH`` does.
    page = sub if any(spec == sub for spec, *_ in keep_alive.DASHBOARD_PAGES) else "overview"
    # An unknown id falls back to the first server, exactly as
    # ``keep_alive._resolve_guild_id`` does, so the select can never point at a
    # server it does not also list.
    guild = next((g for g in GUILDS if g["id"] == guild_id), GUILDS[0])
    # The cards describe the real store, not the seed values in this file.
    _sync_module_cards(guild["id"])
    # The rail and the page's own headings, from the same list and the same copy
    # the route uses — the harness's whole rule is that it renders what production
    # renders, and a rail built here by hand is exactly the drift its own comments
    # warn about.
    title, lede = keep_alive.DASHBOARD_PAGE_COPY.get(
        page, keep_alive.DASHBOARD_PAGE_COPY["overview"]
    )
    nav = keep_alive._page_nav(
        keep_alive.DASHBOARD_PAGES, page, f"?guild_id={guild['id']}",
        groups=keep_alive.DASHBOARD_GROUPS,
    )
    # The roster and the two ledgers, through production's own readers: an epoch
    # is formatted by the route's own helper, and the ledgers come from the tables
    # this harness seeds rather than from a fixture written out here.
    management = preview_management(guild["id"])
    keep_alive._stamp_rows(management["recent"], "at", "when_text")
    if management["roster"]:
        keep_alive._stamp_rows(management["roster"]["members"], "joined_at", "joined_text")
    economy_reading = keep_alive._progression_context(guild["id"], "economy")["economy"]
    level_reading = keep_alive._progression_context(guild["id"], "leveling")["levels"]
    fee = (economy_reading or {}).get("fee") or {}
    # Where a form on the page comes back to: the address with the server in it,
    # which is also what production hands its forms.
    page_path = next(
        path for spec, _label, path, _anchor in keep_alive.DASHBOARD_PAGES if spec == page
    )
    page_url = f"{page_path}?guild_id={guild['id']}"
    # One reading, two readers — the same pair the route hands over: the pages that
    # pick a channel to send a message to, and the log picker's own answer to
    # whether the channel it has stored is one of them.
    channel_choices = keep_alive._channel_choices(guild["id"], page)
    return render_template(
        "dashboard.html",
        # The frame, merged the way the route merges it: `_site_context()` for the
        # name, the counts, the footer and the theme picker, `_invite_context()` for
        # the two invite links. This harness rendered neither, and the shape the
        # omission took was **a wordmark that was a lone "."** — the same defect the
        # route itself was repaired for, alive in the tool a person opens to look at
        # the page. Anything the frame owns is named once, above; the kwargs below
        # are this page's own.
        **{**keep_alive._site_context(), **keep_alive._invite_context(guild_id=guild["id"])},
        guild_id=guild["id"],
        guild_name=guild["name"],
        member_count=guild["members"],
        # …and the page renders for whichever server was asked for.
        bot_ping=int(os.environ.get("PREVIEW_PING", "41")),
        bot_ready=True,
        # The same reading production passes, so this harness cannot render a page
        # the route would not: `gateway` draws the badge both consoles share, and
        # leaving it out is how a template that needs it fails only here.
        #
        # Read from `_BADGE_STATES` rather than `_badge_state()` because this
        # harness has no gateway at all — asking the real one would answer "Bot
        # offline" and the preview would show an offline console, which is not
        # what it is for. `PREVIEW_GATEWAY` picks which of the three to draw, the
        # way `PREVIEW_PING` above picks the latency, so the middle state is
        # reachable here too — it is the one that used to be unreachable, in both
        # directions, and the one worth being able to look at.
        gateway=_preview_badge(),
        # `selling=` used to stand here, read from the same switch the route reads
        # so the topbar's Pricing link could not quietly differ. `_site_context()`
        # above carries it now, which is the *only* version of that argument that
        # cannot drift from the route: one owner, and this file no longer has to be
        # right about which switch it is.
        modules=MODULES,
        # Counted, not seeded: these two were literals from the three-card stub
        # and said "2 / 3" over an eleven-card page after the registry grew.
        active_count=sum(1 for m in MODULES if m["enabled"]),
        total_count=len(MODULES),
        csrf_token="preview-token",
        guilds=[{"id": g["id"], "name": g["name"]} for g in GUILDS],
        can_switch_guild=True,
        # The rail, from the same list the routes are registered from — and this
        # harness never passed it, so the preview's navigation was an empty
        # ``<nav>`` while production drew nine links. A preview that shows none
        # of the navigation cannot show anything about how it is chunked, which
        # is the half of the rail that is a design decision rather than a link.
        # (The console's route below has carried its own version of this for the
        # same reason; this is the dashboard's.)
        dashboard_pages=nav,
        guild_query=f"?guild_id={guild['id']}",
        fleet_url="/servers",
        pages_json=keep_alive._inline_json({
            page_id: {
                "url": f"{path}?guild_id={guild['id']}",
                "label": label,
            }
            for page_id, label, path, _anchor in keep_alive.DASHBOARD_PAGES
        }),
        # A passkey-shaped session (the operator's), which is the one this page
        # renders: no Discord identity in the header, no picker link — but the
        # invite link is gated on the client id alone, exactly as production
        # gates it, so the header button is the real one. The three invite links
        # themselves are `_invite_context()`'s, merged above with the server this
        # page is open on, so `invite_here_url` names the same server the rail does.
        signed_in_as={},
        pick_server_url="",
        # The composer's rulebook, read from the bot rather than restated: the
        # page refuses locally with these sentences and these numbers, so a
        # preview that invented its own would show a different composer. (The
        # template has no defaults on purpose, which is what named this file the
        # moment the view grew the panel.)
        composer_limits={
            "chars": keep_alive.MESSAGE_MAX_CHARS,
            "tags": keep_alive.MESSAGE_MAX_MENTIONS,
            "channels": keep_alive.MESSAGE_MAX_CHANNELS,
            "title": keep_alive.MESSAGE_TITLE_MAX,
        },
        # The real scheduling limits, so the page cannot look right here while the
        # endpoint would refuse what it offers.
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in keep_alive.scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        schedule_catchup_hours=keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        # The real content sources, from the registry the endpoint validates
        # against — without this the picker offered "Message I write" and
        # nothing else, so the one scheduled-message kind that builds its own
        # text could not be reached from the preview at all.
        schedule_content_choices=[
            {"value": "", "label": "Message I write", "blurb": ""},
            *[
                {"value": name, "label": entry["label"], "blurb": entry["blurb"]}
                for name, entry in digest.GENERATORS.items()
            ],
        ],
        server_now=int(time.time()),
        # This server's bot profile. The controls are live so the flow can be
        # driven here, and the POST answers with the harness's honest 503 —
        # there is no gateway to rename anything with. A panel disabled up front
        # would have hidden the form's own error path from the one tool that
        # exists to look at it.
        server_identity={
            "nick": _PREVIEW_IDENTITY["nick"],
            "bio": _PREVIEW_IDENTITY["bio"],
            "has_avatar": False, "has_banner": False,
            "live_name": guild["name"] + " Bot", "live": False, "can_edit": True,
            "max_nick": identity.MAX_NICK, "max_bio": identity.MAX_BIO,
        },
        # The real command-scope numbers, from the same rule the publisher uses
        # and the module state this page is showing.
        command_scope=command_scope.summarise(
            PREVIEW_COMMANDS,
            {module["id"]: module["enabled"] for module in MODULES},
        ),
        # The other half of that panel: what Discord holds. Recorded for real, so
        # the preview shows the line a deployment shows rather than nothing.
        command_scope_publish=record_healthy_publish(guild["id"]),
        # The Status panel. Stubbed rather than read from a live UptimeRobot,
        # because this harness has no key and no network on purpose — and both
        # *are* stubs the page has to tolerate: one row up with a measured
        # ratio, one paused, so the dot colours, the figures and the pills all
        # appear in the preview instead of an empty state that shows none of
        # them. The panel refreshes through the route below, which serves the
        # same shape the real `/api/uptime` does.
        system_status=preview_uptime_snapshot(),
        site_guard_status={"status": "clean", "exposed": 0, "probes": 9},
        config_status={"unset": 22, "attention": 5, "status": "attention"},
        # The rosters and the lockdown ledger, which production reads from the
        # gateway and this process has none of. Written out here for the reason
        # the uptime snapshot above is: the panels' whole job is to show a list and
        # a state, and a preview that showed the *empty* branch of both would show
        # nothing about any of it. The numbers are the harness's own servers.
        management=management,
        # Alone Time's own ledger, as the harness's server would have it. Written
        # out for the reason the rosters above are: the panel exists to show a
        # locked room and the button that gives it back, and the empty branch would
        # show neither. `None` on every page but the Server page, exactly as the
        # route hands it over — the template reads the difference.
        alone_locks=(
            [
                {"channel_id": "1", "channel": "Focus Room", "holder": "Rae",
                 "guests": 0, "turned_away": 2, "held": "12m"},
                {"channel_id": "2", "channel": "Deep Work", "holder": "nobody — quiet hours",
                 "guests": 1, "turned_away": 0, "held": "1h 4m"},
            ]
            if page == "server" else None
        ),
        alone_release_url="/api/alone-time",
        # The page the harness was asked for, and everything the eleven sections
        # are drawn from — all of it read by production's own helpers.
        dash_page=page,
        # One reading, two readers — the same pair the route hands over: the pages
        # that pick a channel to send a message to, and the log picker's own answer
        # to whether the channel it has stored is one of them.
        channel_choices=channel_choices,
        page_title=title,
        page_lede=lede,
        page_anchor=keep_alive._page_anchor(keep_alive.DASHBOARD_PAGES, page),
        page_url=page_url,
        rail=keep_alive._dashboard_rail(nav),
        # The sentence the last write left for this render — popped, exactly as the
        # route pops it, so a reload cannot re-announce an action that already
        # happened. These were the literals ``""``, which is why every form on this
        # page reported nothing: `_form_door` puts the endpoint's own words in the
        # session and production hands them to the template, so a switch that moved
        # and a switch that was refused both came back identical here — and a knob
        # that moves with no confirmation reads as a knob that did not move, which
        # is the opposite of what this harness exists to show.
        dash_notice=keep_alive._plain_marks(session.pop("dash_notice", "")),
        dash_error=keep_alive._plain_marks(session.pop("dash_error", "")),
        economy_view=economy_reading,
        level_view=level_reading,
        economy_display=keep_alive._economy_display(economy_reading),
        level_display=keep_alive._level_display(level_reading),
        economy_fee={
            "label": fee.get("label", ""),
            "enabled": bool(fee.get("enabled", True)),
            "cap": fee.get("cap", 0),
            "stored": bool(fee.get("stored", False)),
            "describe": str(fee.get("describe", "")).replace("**", ""),
        },
        # The queue, the channel picker and the two action lists, from the same
        # readers production uses — the harness has a real seeded store behind it,
        # so an empty stub here would preview a branch production may never take.
        schedule_queue=keep_alive._schedule_queue(guild["id"], page),
        # The moderator log picker: the same reader, the same row, so a preview of
        # the Server page shows the control production draws rather than a form
        # whose dropdown is empty because the harness forgot to read a list. The
        # row itself is this process's copy of the store — unset, which is the
        # state the checklist's one finding is about.
        log_picker=keep_alive._log_picker(guild["id"], channel_choices),
        member_actions=keep_alive.PANEL_MEMBER_ACTIONS,
        server_actions=keep_alive.PANEL_SERVER_ACTIONS,
        toggle_url="/api/toggle",
        toggle_all_url="/api/toggle-all",
        # The Overview's own panel, read by production's own reader rather than
        # written out here. It used to be a hand-built dict, and the radio rebuild
        # is what that cost: the block described a player with a **queue** and a
        # song's *title* and *provider* — the shape of the system that was retired
        # — and the preview would have drawn an empty radio panel with no way to
        # tell a broken page from a stale fixture. ``_seed_radio`` puts a real
        # player in the real registry instead, so the panel is painted by
        # ``keep_alive._overview_live`` from the same shapes production reads,
        # which is the only version of this that cannot drift.
        overview_live=_preview_overview(guild["id"]),
        # The front page's checklist, taken by production's own reader over this
        # harness's switches, its seeded store and its gateway-less server — so
        # the branch where a reading cannot be taken is drawn here rather than
        # assumed, and the one finding the harness's own defaults produce (the
        # moderator log this store has never been told about) is a row a visitor
        # can look at instead of a shape nobody has seen.
        server_health=_preview_health(guild["id"]),
        # The support bridge, for the reason ``server_health`` is here: this
        # harness signs itself in as the operator, the card is drawn only for one,
        # and a preview that skipped it would be a preview of a page the operator
        # never sees. Read through production's own card builder over this
        # process's gateway, so the two branches worth looking at — the address
        # resolving, and the conversation the harness's own store holds — are the
        # ones on screen.
        support=keep_alive._support_card(),
    )


class _PreviewVoice:
    """A voice client for the preview's player: connected, playing, in a room."""

    def __init__(self, channel):
        self.channel = channel
        self._playing = True

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return False

    def stop(self) -> None:
        self._playing = False

    async def disconnect(self, force: bool = False) -> None:
        self._playing = False

    async def move_to(self, channel) -> None:
        self.channel = channel


def _seed_radio(guild_id: int) -> None:
    """Put a real player in the real registry, so the radio panel is a reading.

    Nothing here is a stand-in for the *payload*: ``radio_stream.RadioPlayer`` is
    the object the bot itself builds, the station is a row of the shipped table,
    and only the socket — the one thing this process cannot have — is a stub. The
    panel is then whatever ``status()`` says about it, which is the same object
    the operator console and the dashboard read in production.
    """
    from utils import radio_catalogue, radio_stream

    guild = keep_alive.BOT_INSTANCE.get_guild(guild_id) if keep_alive.BOT_INSTANCE else None
    channels = list(getattr(guild, "voice_channels", []) or [])
    if not channels:
        return
    playing = radio_stream.RadioPlayer(keep_alive.BOT_INSTANCE, int(guild_id))
    playing.voice_client = _PreviewVoice(channels[0])
    # A station somebody asked for by name, so the card shows a genre, a language
    # and a country rather than the first row of the file.
    playing.current = next(
        (row for row in radio_catalogue.CATALOGUE if row.country and row.language),
        radio_catalogue.CATALOGUE[0],
    )
    playing.requester = "Wren"
    playing.engine = "ffmpeg encode 128 kbps"
    playing.stay = True
    playing._started_at = time.monotonic() - 42.0


def _preview_overview(guild_id: int) -> dict:
    """The Overview panel, read by production's own reader."""
    _seed_radio(guild_id)
    return keep_alive._overview_live(guild_id)


def _preview_health(guild_id: int) -> dict:
    """The Overview's checklist, read by production's own reader.

    The module states are this harness's own switches, the publish reading is the
    healthy record it already writes, and everything else the reader takes off the
    server object — which here has no ``me``, so the one reading this harness
    cannot take is drawn as *not read* rather than as a clean answer. That is the
    branch worth having in a preview: it is the difference the card exists for.
    """
    return keep_alive._server_health(
        guild_id,
        {module["id"]: module["enabled"] for module in MODULES},
        record_healthy_publish(guild_id),
    )


#: What the Members and Server panels show in the preview: a roster with the
#: shapes that have to render (a bot, somebody timed out, a plain member), a ban
#: with and without a reason, and a lockdown holding two channels. Deliberately
#: mixed rather than uniform — a list of five identical rows hides every branch.
PREVIEW_MEMBERS = (
    {"id": 424242424242424242, "display_name": "Wren", "bot": False, "timeout_seconds": 0, "top_role": "@admin"},
    {"id": 515151515151515151, "display_name": "DaCat", "bot": True, "timeout_seconds": 0, "top_role": "@bot"},
    {"id": 626262626262626262, "display_name": "Kite", "bot": False, "timeout_seconds": 5400, "top_role": "@everyone"},
    {"id": 737373737373737373, "display_name": "Sorrel", "bot": False, "timeout_seconds": 0, "top_role": "@mod"},
)


def preview_management(guild_id: int) -> dict:
    """The two panels' context, in the shape ``keep_alive._management_context``
    returns.

    Deliberately the *same keys*, including the `read` flag: the page branches on
    ``mgmt.read``, so a fixture that quietly supplied the roster and not the flag
    would preview the branch production takes for a server it could not read.
    """
    guild = next((g for g in GUILDS if g["id"] == guild_id), GUILDS[0])
    return {
        "page": "",
        "read": True,
        "why": "",
        "roster": {
            "members": list(PREVIEW_MEMBERS),
            "matched": len(PREVIEW_MEMBERS),
            "shown": len(PREVIEW_MEMBERS),
            "cache": len(PREVIEW_MEMBERS),
            "queried": False,
            "complete": True,
        },
        "bans": [
            {"id": 848484848484848484, "name": "Ash", "display_name": "Ash", "reason": "raid coordination"},
            {"id": 959595959595959595, "name": "Bramble", "display_name": "Bramble", "reason": ""},
        ],
        "control": {
            "locked": 2,
            "ledger": True,
            "channels": [
                {"id": 100000000000000001, "name": "general"},
                {"id": 100000000000000002, "name": "off-topic"},
            ],
            "counts": {"text": 3, "voice": 2, "manageable": 3},
            "plan": f"On **{guild['name']}** that is 3 of 5 text channels I can manage; 2 are voice and are not touched.",
        },
        "query": "",
        "recent": [
            {"action": "kick", "actor_name": "Preview Admin", "target": "Ash", "detail": "Kicked — case #11.", "at": 1774000000.0},
            {"action": "lockdown", "actor_name": "Preview Admin", "target": "", "detail": "Locked 2 channels.", "at": 1773990000.0},
        ],
    }


#: What the preview's Status panel shows. Deliberately two monitors in two
#: different states — the defect this guards against is a panel that renders one
#: state and silently drops the other.
PREVIEW_UPTIME: dict = {
    "status": "attention",
    "at": None,
    "page": "",
    "monitors": [
        {
            "id": "803978677",
            "name": "DaCat Bot",
            "url": "https://meow-discord-bot.onrender.com",
            "status": 2,
            "label": "up",
            "up": True,
            "down": False,
            "paused": False,
            "ratios": {"1d": 100.0, "7d": 99.99, "30d": 99.97},
            "response_ms": 157.0,
        },
        {
            "id": "803978678",
            "name": "Public site",
            "url": "https://dacat.pages.dev",
            "status": 0,
            "label": "paused",
            "up": False,
            "down": False,
            "paused": True,
            "ratios": {},
            "response_ms": None,
        },
    ],
    "total": 2,
    "up": 1,
    "down": 0,
    "paused": 1,
    "waiting": 0,
    "ratio_30d": 99.97,
    "detail": "1 monitor is paused, so nothing is watching that address.",
}

#: When this process "read" the stub. Production's panel words the age of its
#: last reading — "read 2 minutes ago" — so a stub with a fixed `at` of ``None``
#: pinned that line to "not read yet in this process" through every Refresh, which
#: made the panel's own refresh look like it did nothing. Seeded a little in the
#: past so the line has something truthful to say as soon as the page opens.
_PREVIEW_READ_AT: int = int(time.time()) - 95


def preview_uptime_snapshot() -> dict:
    """The stub, carrying the fields the real snapshot always carries.

    ``age_text`` is produced by :func:`utils.uptime_status.age_text` — the same
    function production uses — rather than spelled again here, because the whole
    class of defect this harness keeps producing is a hand-written stub that
    agrees with the real endpoint about the shape and disagrees about a field.
    """
    age = max(0, int(time.time()) - _PREVIEW_READ_AT)
    return {
        **PREVIEW_UPTIME,
        "at": _PREVIEW_READ_AT,
        "age_seconds": age,
        "age_text": uptime_status.age_text(age),
    }


@app.route("/market")
def market_index():
    """Bare ``/market`` — production redirects it to the first instrument.

    The dashboard's own 📈 Market button lands here, so a preview that answered
    Flask's 404 made the one link to the charts look broken.
    """
    return redirect("/market/BTC")


@app.route("/login/discord")
def discord_sign_in():
    """Honest refusal: this process cannot do a real OAuth round trip."""
    return (
        "<h1>Preview harness — Discord sign-in</h1>"
        "<p>Signing in with Discord needs a registered redirect URL and a client "
        "secret, and this harness has neither: it never talks to Discord. Run the "
        "bot with <code>DISCORD_CLIENT_ID</code> and "
        "<code>DISCORD_CLIENT_SECRET</code> set to exercise the real redirect.</p>"
        f"<p>The callback URL this host would register is "
        f"<code>{html.escape(keep_alive.oauth_redirect_uri())}</code>.</p>"
        "<p><a href='/login'>Back to sign-in</a></p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.route("/servers", methods=["GET", "POST"])
def servers_page():
    """The picker in both of its shapes, so its layout and invite can be checked.

    ``?as=user`` is what somebody who signed in with Discord sees; the default is
    the operator's, which is the same page over every server the bot is in with
    what each one is running. The numbers come from ``_module_summary`` — the real
    function, reading the harness's copy of the database — so a preview cannot
    show a count that production would not.

    A POST really does switch, because that is what the page does: **Manage**
    posts the server it names and lands in that server's dashboard. It used to be
    answered with the same page — "this harness holds no session, so there is
    nothing to switch" — and the result was a button that visibly did nothing.
    """
    operator = (request.args.get("as") or "operator").strip().lower() != "user"
    error = None
    if request.method == "POST":
        payload = request.get_json(silent=True) or request.form
        try:
            chosen = int(payload.get("guild_id"))
        except (TypeError, ValueError):
            chosen = None
        if chosen is None or not any(g["id"] == chosen for g in GUILDS):
            # The real route's own words, and for the same reason: the id came
            # from a form, so it is a request and not a fact.
            error = "You do not manage that server."
        else:
            CURRENT_GUILD["id"] = chosen
            return redirect(f"/dashboard?guild_id={chosen}")
    servers = [{"id": str(g["id"]), "name": g["name"], "icon": ""} for g in GUILDS]
    if operator:
        for entry in servers:
            entry["scope"] = keep_alive._module_summary(int(entry["id"]))
    body = render_template(
        "servers.html",
        servers=servers,
        error=error,
        operator=operator,
        user={} if operator else {"id": "1", "name": "Preview Admin", "avatar": ""},
        invite_url=discord_auth.invite_url(),
        invite_limited_url=discord_auth.invite_url(minimal=True),
        # Which server is open, so the picker says **Open** where you already are
        # and **Manage** on the others — the same distinction production draws
        # from the session, and the reason the button choice is worth looking at.
        current_guild_id=int(CURRENT_GUILD["id"]),
        csrf_token="preview-token",
    )
    return (body, 403) if error else body


def preview_figures() -> dict:
    """The payload `/api/figures` serves, over the harness's own servers.

    Built by the real :func:`utils.figures.deployment` with a gateway made from
    the harness's guild list — the one thing this process cannot have is Discord,
    and a card that renders four em dashes would show nothing about the card.
    Called rather than cached, because the harness answers every request with the
    state it has *now*. """
    return keep_alive.figures.deployment(gateway={
        "servers": len(GUILDS),
        "members": sum(guild["members"] for guild in GUILDS),
        "systems": len(MODULES),
        "commands": len(PREVIEW_COMMANDS),
        "connected": True,
    })


#: The console's API, mounted from the real app rather than described again.
#:
#: This route used to hand-build the console's page context, so every number the
#: preview showed was a second implementation of the number production shows — and
#: a preview that re-implements its page can be wrong in exactly the way the page
#: cannot. The console is a bundled app that fetches `/api/console/*`, and those are
#: `keep_alive`'s own views, so the harness borrows them by rule. The database they
#: read is the harness's copy, which is the whole point of previewing it here.
for _rule in keep_alive.app.url_map.iter_rules():
    if str(_rule).startswith("/api/console/"):
        app.add_url_rule(
            str(_rule),
            endpoint=f"preview{_rule.endpoint}",
            view_func=keep_alive.app.view_functions[_rule.endpoint],
            methods=sorted(_rule.methods - {"HEAD", "OPTIONS"}),
        )


def _sign_in() -> None:
    """Sign this harness's own browser in, the way a real sign-in does.

    Four keys and one ledger row, in one place, because the *ledger row* is the
    half that was missing: ``keep_alive.is_authenticated`` asks Flask's cookie
    whether this server signed the session **and** ``utils.sessions`` whether the
    row is still believed, and the preview's session cookie outlives the process
    that minted it — the harness makes its own copy of the database at import, so
    the sid in a returning browser's cookie names a row the new process has never
    heard of and every delegated write answers ``401 Unauthorized``. That is not a
    rare state: it is what every second run of this file looks like in a browser
    that has already been here, and the shape it took was a rates form that
    reported *Unauthorized* on a page that had just shown the saved rates.

    ``keep_alive._record_session`` is the fix rather than a new sid written here:
    it keeps a live sid (so the Access page does not grow a second row for one
    browser) and replaces a dead one, which is the same rule production follows
    when the passkey is entered twice.
    """
    session["authenticated"] = True
    session["auth_scope"] = "global"
    session["csrf_token"] = "preview-token"
    keep_alive._record_session(label="this browser")


@app.route("/owner")
@app.route("/owner/<path:sub>")
def owner_console(sub: str = ""):
    """The console's own pages, plus the session its API requires — *delegated*.

    Production gates `/owner` and every `/api/console/*` route on the operator's
    session. This harness has no sign-in at all, so the preview signs itself in —
    which is honest rather than a hole: the process binds to loopback, it is the
    operator's own machine, and without it the app would load, be answered 401,
    and show a sign-in error instead of the page this harness exists to make
    lookable-at.

    What it draws is ``keep_alive.owner_console`` itself, for the reason this
    file delegates `/api/level-settings` and the console's own API: a second copy
    of a page's context is a copy that goes stale silently, and the console is
    the surface where *"the preview shows what production would"* matters most —
    it is the one screen an operator opens on purpose.

    It used to render ``templates/console.html``: the shell of the bundled React
    app, which `/owner` no longer serves at all. So the address a playtest opens
    to look at the operator's console drew the *archived* one — every control it
    appeared to offer belonged to an app the backend had already stopped routing,
    and none of the panel that replaced it could be looked at. The archived shell
    is still in the tree (its own laws read it), which is exactly why rendering
    it here was so easy to leave behind: it answered 200.
    """
    _sign_in()
    if (sub or "").strip("/") == "god":
        # The tier is the owner's passkey and a quarter of an hour in production,
        # and this harness has neither to type — so the god page would be drawn
        # *locked*, with every control on it disabled, which is a page a playtest
        # can read and not drive. Opening the tier is the same choice `_sign_in`
        # above makes for the session itself, and it is bounded the same way: the
        # loopback address, the operator's own machine, and a database copy.
        session["god_until"] = time.time() + keep_alive.GOD_UNLOCK_SECONDS
    return keep_alive.owner_console(sub)


@app.route("/owner/god", methods=["POST"])
def owner_god_action():
    """The tier's own four actions, delegated with the tier held open.

    The page draws them as forms that POST here. Without this route a press in the
    harness reaches ``keep_alive``'s real one with no session, which answers a
    redirect to a sign-in this process does not have — a preview that shows four
    controls and demonstrates none of them, which is the defect the console and
    broadcast delegations above were both written for. The actions run against the
    stand-in gateway and the copy of the database like everything else here.
    """
    _sign_in()
    session["god_until"] = time.time() + keep_alive.GOD_UNLOCK_SECONDS
    return keep_alive.owner_god_action()


@app.route("/owner/god/broadcast", methods=["GET", "POST"])
def owner_god_broadcast():
    """The fleet announcement's two verbs, delegated, with the tier held open.

    The panel's dry run is a GET and its send is a POST, and both are gated on the
    tier being *open* — which in production is the owner's passkey and a quarter of
    an hour. This harness has no passkey to type, so it opens the tier the way the
    passkey does (`god_until`, the key `_god_unlocked` reads) before delegating.
    Without it the panel would draw on `/owner/god` and answer every press of its
    two buttons with a redirect to a sign-in this process does not have — a preview
    that shows a control it cannot demonstrate, which is the defect the console
    delegation above was written for.
    """
    _sign_in()
    session["god_until"] = time.time() + keep_alive.GOD_UNLOCK_SECONDS
    return keep_alive.owner_god_broadcast()


@app.route("/logout", methods=["GET", "POST"])
def no_session():
    """Say there is no session here rather than 404-ing on a visible button.

    The rendered dashboard carries the real header, Sign out included, so a
    playtester clicks it — and a 404 reads as "the dashboard is broken" when the
    truth is that this harness has no sign-in to begin with.
    """
    return (
        "<h1>Preview harness — no session</h1>"
        "<p>This page renders the real dashboard's templates with stand-in data; "
        "it has no session, so there is nothing to sign out of."
        "</p>"
        "<p><a href='/dashboard'>Back to the dashboard</a></p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.route("/market/<symbol>")
def market_page(symbol):
    """The real market route, so the chart can be checked under the real CSP."""
    from utils import market as market_module

    instrument = market_module.BY_SYMBOL.get(str(symbol).strip().upper())
    if instrument is None:
        # Production's own refusal, in production's own words: a misspelt symbol
        # is a thing people do, and "404 nope" tells them nothing. Same sentence,
        # same way out, so the preview cannot flatter the real page.
        #
        # ``html.escape`` rather than a chain of ``.replace()``s. The chain that
        # was here escaped ``&`` *after* ``<`` and ``>``, so ``a<b`` came back as
        # ``a&amp;lt;b`` and the page showed the entity instead of the character —
        # a misspelt symbol is exactly the input that reports this, and getting it
        # wrong made the harness lie about a page it exists to render faithfully.
        safe = html.escape(str(symbol), quote=True)
        return (
            f"<h1>404 — {safe} is not on the market</h1>"
            "<p><a href='/market/BTC'>Open BTC instead</a></p>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    # The route's own context builder, not a second copy of it: the preview exists
    # to render what the live page renders, and a harness that assembled its own
    # context would go on working the day the real one grew a key it needs.
    import keep_alive

    # The route's own merge, `_site_context()` included: the market page wears the
    # site frame like every other address, and rendering it with only the market's
    # half left the wordmark a lone "." and the `<title>` ending on its separator.
    return render_template(
        "market.html", **{**keep_alive._site_context(), **keep_alive.market_context(instrument)}
    )


#: A few plausible prices for the preview board, in USD.
_PREVIEW_PRICES = {"BTC": 96400.0, "ETH": 3180.0, "SOL": 182.0, "XRP": 2.15, "BNB": 610.0}


def _preview_snapshot():
    """A small, real snapshot — built by the code the live endpoint calls.

    The shapes here are the endpoint's, not this file's: a hand-written
    ``{"quotes": {}}`` looked fine and was not. The page does
    ``quotes.quotes.find(...)``, so an object threw a TypeError that its own
    ``catch`` reported as "could not reach the bot" — a permanent outage drawn
    over a page whose every request had answered 200. Going through
    ``quotes_payload`` means the preview cannot drift from production again.
    """
    now = time.time()
    quotes = {
        instrument.symbol: market_module.Quote(
            symbol=instrument.symbol,
            price_usd=_PREVIEW_PRICES.get(instrument.symbol, 100.0 + index),
            change_pct=((index % 7) - 3) * 0.75,
            source="preview",
            # Fresh enough to be tradable, so the board shows a live page rather
            # than the paused one.
            as_of=now - 12,
        )
        for index, instrument in enumerate(market_module.INSTRUMENTS[:5])
    }
    return market_module.Snapshot(quotes)


def _preview_series(symbol: str):
    """A short history shaped the way the bot's own polling is: flat points.

    Labelled as observed rather than as exchange OHLC on purpose — the page
    prints that label, and claiming "real candle data" over points nobody traded
    is the kind of thing this project keeps taking out.
    """
    now = time.time()
    start = _PREVIEW_PRICES.get(symbol, 100.0)
    candles = []
    for i in range(60):
        price = round(start * (1 + ((i % 11) - 5) / 100.0), 4)
        candles.append(market_module.Candle(
            ts=now - (60 - i) * 86400,
            open=price, high=price, low=price, close=price,
        ))
    return market_module.CandleSeries(
        symbol=symbol, candles=tuple(candles),
        source="this bot's own polling", axis_label="observed prices",
    )


@app.route("/api/market/quotes")
def api_market_quotes():
    return jsonify(market_module.quotes_payload(_preview_snapshot(), time.time()))


@app.route("/api/market/candles")
def api_market_candles():
    symbol = str(request.args.get("symbol") or "BTC").strip().upper()
    period = str(request.args.get("period") or market_module.DEFAULT_PERIOD)
    if period not in market_module.PERIODS:
        period = market_module.DEFAULT_PERIOD
    return jsonify(market_module.series_payload(symbol, _preview_series(symbol), period))


@app.route("/api/channels")
def api_channels():
    return jsonify({"success": True, "guild_id": 777001, "channels": CHANNELS})


#: The last member action the page asked for, so a person previewing this can read
#: back exactly what the browser would have sent — the same reason ``LAST_SEND``
#: exists below. Nothing is performed: this process has no gateway, and a harness
#: that *did* the thing would be a second implementation of the feature.
LAST_MEMBER_ACTION: dict = {}


@app.route("/api/member-action", methods=["POST"])
@keep_alive._form_door
def api_member_action():
    """A recorded no-op. The panel, the wording and the payload, not the action."""
    data = keep_alive._json_or_form()
    LAST_MEMBER_ACTION.clear()
    LAST_MEMBER_ACTION.update(data)
    action = str(data.get("action") or "")
    names = {
        "warn": "Warn", "timeout": "Mute", "timeout_remove": "Unmute",
        "kick": "Kick", "ban": "Ban", "unban": "Unban", "clear_warnings": "Warnings cleared",
    }
    return jsonify({
        "success": True,
        "action": action,
        "verb": names.get(action, action.title()),
        "case_id": 12,
        "said": f"{names.get(action, action.title())} — case #12. (preview: nothing was performed)",
    })


@app.route("/api/member-warnings")
def api_member_warnings():
    return jsonify({
        "success": True,
        "warnings": [
            {"id": 7, "reason": "posting invites"},
            {"id": 9, "reason": "caps in #general"},
        ],
    })


@app.route("/api/server-action", methods=["POST"])
@keep_alive._form_door
def api_server_action():
    """The same idea one level up: say what would have happened, do nothing."""
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "")
    said = {
        "lockdown": "Locked **3** channel(s). The 2 voice channels are not text.",
        "unlock": "Restored **3** channel(s) to what they were before.",
        "slowmode": f"Set slowmode on **3** channel(s) to {int(data.get('seconds') or 0)}s.",
    }.get(action, "Nothing to do.")
    return jsonify({
        "success": True,
        "action": action,
        "changed": 3,
        "said": f"{said} (preview: nothing was performed)",
    })


@app.route("/api/mentionables")
def api_mentionables():
    query = (request.args.get("q") or "").strip().lower()
    if not query:
        return jsonify({"success": True, "guild_id": 777001, "users": [], "roles": ROLES, "special": SPECIAL})
    users = [m for m in MEMBERS if query in m["label"].lower() or query in m["sub"].lower()]
    roles = [r for r in ROLES if query in r["label"].lower()]
    special = SPECIAL if query in ("@", "every", "here", "e") else []
    return jsonify({"success": True, "guild_id": 777001, "users": users, "roles": roles, "special": special})


#: Whatever the browser last posted, so a human — or an agent driving the page
#: with a browser — can read the ids it handed back instead of guessing.
LAST_SEND: dict = {}


def _sync_module_cards(guild_id) -> None:
    """Point this file's module cards at the **real** module store.

    The switch used to flip the in-memory stub and write nothing down, while the
    command-scope panel and the ``/servers`` list read the real cache — so the
    same page could show two different answers about the same system, and only
    the stub was believed. Writing through the real store (safe here: this
    process works on its own copy of the database, made at import) makes every
    panel agree, and makes the preview show what production would instead of
    what a stub was seeded with.
    """
    from utils import modules

    try:
        states = modules.get_states(int(guild_id))
    except Exception as exc:
        print(f"[PREVIEW] could not read module state: {exc}", flush=True)
        return
    for module in MODULES:
        if module["id"] in states:
            module["enabled"] = bool(states[module["id"]])


@app.route("/api/server-settings", methods=["POST"])
def api_server_settings():
    """The moderator log picker, answered by the endpoint that owns the write.

    Delegated for the reason ``/api/level-settings`` is: the row, the two
    refusals and the sentence about whether the bot can actually post there are
    ``keep_alive``'s, and a second copy here would be a preview that saves into a
    store production does not read. The harness has no gateway member in its
    server object, so the branch this draws is the honest middle one — the
    channel is set, and whether Discord would take a card there was not answered.
    """
    _sign_in()
    return keep_alive.api_server_settings()


@app.route("/api/module-settings", methods=["POST"])
def api_module_settings():
    """A system's own settings form, answered by the endpoint that owns the write.

    Delegated for the reason ``/api/server-settings`` is: the declarations, the
    bounds and the sentences are ``keep_alive``'s, and a second copy here would
    be a preview that saves a threshold production does not read. The real route
    is gated on a session this process cannot obtain, so the harness signs in
    first — the same move every other write on this page makes.
    """
    _sign_in()
    return keep_alive.api_module_settings()


@app.route("/api/alone-time", methods=["POST"])
def api_alone_time():
    """Releasing a locked voice room, answered by the endpoint that owns the write.

    Delegated for the reason the settings forms are: the ledger, the restore and
    the sentences are ``keep_alive``'s and ``utils/alonetime``'s, so a second copy
    here would be a preview that reported giving a room back while production's
    ledger still held it. The harness's stand-in bot has no gateway loop, so the
    branch this draws is the honest middle one — the request reached the endpoint,
    and whether Discord would take the restore was not answered.
    """
    _sign_in()
    return keep_alive.api_alone_time()


@app.route("/api/reaction-roles", methods=["POST"])
def api_reaction_roles():
    """The reaction-role mappings form, delegated like the settings forms.

    The harness's stand-in bot has no gateway loop, so the branch this draws is
    the honest middle one — the mapping is saved, and adding the reaction
    myself could not be answered.
    """
    _sign_in()
    return keep_alive.api_reaction_roles()


@app.route("/api/level-settings", methods=["POST"])
def api_level_settings():
    """The rates form, answered by the endpoint that owns the write.

    Delegated rather than restated, like ``/``, ``/login`` and the console's own
    API: the clamps, the two refusals and the sentence are ``keep_alive``'s, so a
    preview can never show a band the bot would not pay. A hand-built copy here
    would be the third place "50 coins a minute" could be written down.

    The sign-in is the harness's, not production's — the same one
    ``owner_console`` performs, and for the same reason. The real route is gated
    on a session this process cannot obtain, and without it the one control on
    ``/dashboard/leveling`` would answer 401 in the one tool whose job is showing
    what the page does.
    """
    _sign_in()
    return keep_alive.api_level_settings()


@app.route("/api/toggle", methods=["POST"])
@keep_alive._form_door
def api_toggle():
    """Flip one module, through the same store the real endpoint writes to.

    The payload is read with the app's own adapter, `_json_or_form`, and the
    answer goes through the app's own form door for the same reason: the switch
    on `/dashboard/systems` is a **form** — the canonical direction of the one
    shared control — so a harness that only understood JSON answered every press
    of it with `400 No guild available` and no redirect, which is a tab full of
    JSON and a preview that cannot demonstrate the one control it exists to
    show. This is not a second implementation: it is the endpoint's own read and
    the endpoint's own redirect, called here rather than copied.

    Missing until a playtest drove the switches in a browser: every toggle was
    answered with the Flask 404 page, so the dashboard's headline control showed
    a red "Request failed (404)" toast and a switch that sprang back — the page
    looked broken when only the harness was.
    """
    from utils import modules

    payload = keep_alive._json_or_form()
    if payload.get("guild_id") is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    module_id = payload.get("module")
    enabled = bool(payload.get("enabled"))
    module = next((m for m in MODULES if m["id"] == module_id), None)
    if module is None:
        # The real route's own refusal, status and words: an unknown module is a
        # 400 "Unknown module", and a harness that answers differently teaches
        # the page's error path the wrong lesson.
        return jsonify({"success": False, "error": "Unknown module"}), 400
    if not modules.set_enabled(int(payload["guild_id"]), module_id, enabled):
        # The real route refuses to report success for a change it did not save.
        return jsonify({"success": False, "error": "Could not save that change. Please try again."}), 500
    _sync_module_cards(payload["guild_id"])
    LAST_TOGGLE.clear()
    LAST_TOGGLE.update({"module": module_id, "enabled": enabled})
    # The two fields the console reads back, computed the way the real route
    # computes them. Omitted until a playtest flipped a switch here: the page
    # types this answer as carrying `republish`, so an answer without it made
    # every preview toggle warn *"saved, but the command list was not
    # republished — the bot may be restarting"* — the warning path, from a
    # gateway that never restarts, on the one screen this harness exists to show.
    guild = int(payload["guild_id"])
    return jsonify({
        "success": True,
        "guild_id": payload.get("guild_id"),
        "module": module_id,
        "enabled": enabled,
        "sync_latency_ms": 0,
        "publish_at": keep_alive._last_publish_at(guild),
        "republish": keep_alive._resync_commands(guild),
    })


@app.route("/api/toggle-all", methods=["POST"])
@keep_alive._form_door
def api_toggle_all():
    """Enable or disable every module at once — the real route's shape."""
    from utils import modules

    payload = keep_alive._json_or_form()
    if payload.get("guild_id") is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    enabled = bool(payload.get("enabled"))
    # Every module the real registry can toggle, not just the three cards this
    # page happens to show: "disable all" is what an operator presses when the
    # bot looks broken, and a harness where it only reaches the visible three
    # would hide exactly the state that matters.
    failed = modules.set_many(
        int(payload["guild_id"]), {mid: enabled for mid in keep_alive.ALLOWED_MODULE_IDS}
    )
    if failed:
        return jsonify({
            "success": False,
            "error": f"Could not save: {', '.join(failed)}",
            "guild_id": payload.get("guild_id"),
            "enabled": enabled,
        }), 500
    _sync_module_cards(payload["guild_id"])
    LAST_TOGGLE.clear()
    LAST_TOGGLE.update({"module": "*", "enabled": enabled})
    # Same pair as the single switch above, for the same reason: the bulk action
    # words its own toast off `republish`, so a payload without it reports every
    # "all on" as a command list Discord was never told about.
    guild = int(payload["guild_id"])
    return jsonify({
        "success": True,
        "guild_id": payload.get("guild_id"),
        "enabled": enabled,
        "sync_latency_ms": 0,
        "publish_at": keep_alive._last_publish_at(guild),
        "republish": keep_alive._resync_commands(guild),
    })


#: What the preview's identity form is holding. In memory only, and for display:
#: a save cannot reach Discord from here, which the endpoint says out loud.
_PREVIEW_IDENTITY: dict = {"nick": "", "bio": ""}


@app.route("/api/uptime")
def api_uptime():
    """The same rows the panel rendered, for the panel's Refresh button.

    The real route answers from a background loop's snapshot; this one answers
    the stub above, stamped with *now* so the "read N seconds ago" line moves
    the way it does in production.
    """
    return jsonify({
        "success": True,
        "configured": False,
        **preview_uptime_snapshot(),
        "detail": (
            "Not connected: this is the preview harness, so there is no "
            "UPTIMEROBOT_API_KEY and these two rows are stand-ins."
        ),
    })


@app.route("/api/figures")
def api_figures():
    """The deployment's own counts, for the cards' Read again button.

    Uncached here, unlike production's minute: the harness is asked by one
    person at a time and its whole job is to show what a click does.
    """
    payload = preview_figures()
    payload["at"] = int(time.time())
    return jsonify(payload)


@app.route("/api/command-scope")
def api_command_scope():
    """This server's own command list, from the real rule."""
    if request.args.get("guild_id") is None:
        return jsonify({"success": False, "error": "No guild available"}), 400
    states = {module["id"]: module["enabled"] for module in MODULES}
    summary = command_scope.summarise(PREVIEW_COMMANDS, states)
    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        guild_id = CURRENT_GUILD["id"]
    return jsonify({
        "success": True,
        "guild_id": request.args.get("guild_id"),
        # Served the same way the real route serves it, and recorded through the
        # real ledger: the refresh has to paint the same sentence the view did.
        "publish": record_healthy_publish(guild_id),
        **summary,
    })


@app.route("/api/identity", methods=["POST"])
@keep_alive._form_door
def api_identity():
    """The preview cannot rename anything, and says exactly that.

    The profile lives on the bot's own member in one server, which needs a
    gateway; a preview that answered success would be teaching the page's happy
    path something that never happened.
    """
    return jsonify({
        "success": False,
        "error": (
            "This is the preview — it has no connection to Discord, so it cannot "
            "rename the bot or change its picture. Run the bot and open the real "
            "dashboard to do that."
        ),
    }), 503


@app.route("/api/identity/image/<kind>")
def api_identity_image(kind: str):
    """No stored picture in a preview: the panel shows its placeholder."""
    return jsonify({"success": False, "error": "Nothing set for this server"}), 404


@app.route("/__last-toggle")
def last_toggle():
    """What the page last asked for — the same shape as ``/__last-send``."""
    return jsonify(LAST_TOGGLE)


@app.route("/api/send-message", methods=["POST"])
@keep_alive._form_door
def api_send_message():
    """Resolve the posted ids the way the real endpoint does.

    An id the page rounded is an id nothing matches, and the answer says so in
    the same words the bot uses — which is the whole failure this harness is
    here to make visible. The *validation* is the real function
    (``keep_alive._build_message``), for the reason the scheduling endpoints call
    ``_schedule_timing``: this route used to answer ``success: true`` to whatever
    it was handed, so a 2,100-character body came back as "SENT 13:24" with a
    receipt where the bot answers *"That message is 2100 characters; the limit is
    2000."* and posts nothing — a preview teaching the page's happy path an
    outcome production refuses.
    """
    data = keep_alive._json_or_form()
    payload, channel_ids, error = keep_alive._build_message(_PreviewGuild(), data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    LAST_SEND.clear()
    LAST_SEND.update(data)
    by_id = {c["id"]: c for c in CHANNELS}
    sent, failed = [], []
    for cid in channel_ids:
        channel = by_id.get(str(cid))
        if channel is None:
            failed.append({"channel_id": cid, "name": None, "error": "That channel no longer exists"})
        else:
            sent.append({
                "channel_id": cid,
                "name": channel["name"],
                "jump_url": f"https://discord.com/channels/777001/{cid}/1",
            })
    # The real endpoint files every immediate send as a finished history row
    # (``utils.scheduled.record_sent``); mirrored here so the page's "sent now"
    # row can be seen and driven in a browser.
    results = [
        {"channel_id": item["channel_id"], "name": item["name"], "ok": True,
         "error": "", "jump_url": item["jump_url"]}
        for item in sent
    ] + [
        {"channel_id": item["channel_id"], "name": item["name"], "ok": False,
         "error": item["error"], "jump_url": ""}
        for item in failed
    ]
    now = int(time.time())
    SCHEDULE.append({
        "id": max([r["id"] for r in SCHEDULE] + [0]) + 1,
        "channel_ids": [str(item["channel_id"]) for item in results],
        # The payload the real builder made, not a hand-rolled one: it is what
        # carries the tags' resolved text and the mentions Discord is allowed to
        # notify, and the page renders its preview from it.
        "payload": payload,
        "preview": " ".join(str(payload.get("content") or "").split()) or "(an embed)",
        "run_at": now,
        "repeat_minutes": 0,
        "source": "now",
        "status": "sent" if sent else "failed",
        "created_by_name": "RAJ",
        "last_run_at": now,
        "last_error": "" if not failed else failed[0]["error"],
        "results": results,
        "fire_count": 1,
    })
    return jsonify({
        "success": bool(sent),
        "results": [],
        "sent": sent,
        "failed": failed,
        "ignored_mentions": payload.get("ignored_mentions", []),
    })


#: Whatever the browser last posted to the scheduling endpoints, and a small
#: in-memory queue behind them, so the page's scheduling path can be driven end to
#: end. The validation is the *real* function (``keep_alive._schedule_timing``), so
#: this harness shows the wording the bot actually answers with.
LAST_SCHEDULE: dict = {}
SCHEDULE: list = []


def _seed_queue():
    now = int(time.time())
    SCHEDULE.append({
        "id": 1,
        "channel_ids": ["1548437081120833657", "1548437081120833658"],
        "payload": {"content": "@everyone Weekly town hall starts in an hour", "everyone": True},
        "preview": "@everyone Weekly town hall starts in an hour",
        "run_at": now + 6 * 3600,
        "repeat_minutes": 10080,
        "source": "schedule",
        "status": "pending",
        "created_by_name": "RAJ",
        "last_run_at": now - 86400,
        "last_error": "1 channel(s) refused it: I cannot send messages in #mod-logs",
        # A real row carries the name for anything it could resolve (see
        # ``keep_alive``'s per-channel results); only the two "I cannot find that
        # channel" refusals have no name to carry. Seeding a *successful*
        # channel without one made the page's id fallback look like a defect in
        # a browser, when it is the honest answer for a channel that is gone.
        "results": [
            {"channel_id": "1548437081120833657", "name": "general", "ok": True,
             "error": "",
             "jump_url": "https://discord.com/channels/777001/1548437081120833657/1"},
            {"channel_id": "1548437081120833661", "name": "mod-logs", "ok": False,
             "error": "I cannot send messages in #mod-logs", "jump_url": ""},
        ],
        "fire_count": 3,
    })
    SCHEDULE.append({
        "id": 2,
        "channel_ids": ["1548437081120833663"],
        "payload": {"content": "Movie night in the voice channel"},
        "preview": "Movie night in the voice channel",
        "run_at": now + 400,
        "repeat_minutes": 0,
        "source": "schedule",
        "status": "pending",
        "created_by_name": "DaCat",
        "last_run_at": None,
        "last_error": "",
        "results": [],
        "fire_count": 0,
    })


_seed_queue()


def _schedule_view(row: dict) -> dict:
    by_id = {c["id"]: c for c in CHANNELS}
    channels = []
    for cid in row["channel_ids"]:
        channel = by_id.get(str(cid))
        channels.append({
            "id": str(cid),
            "name": channel["name"] if channel else None,
            "kind": channel["kind"] if channel else "",
            "exists": bool(channel),
        })
    repeat = dict(keep_alive.scheduled.REPEAT_CHOICES).get(row["repeat_minutes"], "Once")
    return {
        **row,
        "channels": channels,
        "repeat": repeat,
        "has_everyone": "@everyone" in str(row["payload"].get("content") or ""),
        "channel_count": len(channels),
    }


@app.route("/api/scheduled")
def api_scheduled():
    return jsonify({
        "success": True,
        "guild_id": 777001,
        "server_now": int(time.time()),
        "upcoming": [_schedule_view(r) for r in SCHEDULE if r["status"] in ("pending", "sending")],
        "recent": sorted(
            (_schedule_view(r) for r in SCHEDULE if r["last_run_at"]),
            key=lambda r: r["last_run_at"],
            reverse=True,
        ),
        "pending": sum(1 for r in SCHEDULE if r["status"] == "pending"),
        "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        "catchup_hours": keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
    })


@app.route("/api/schedule-message", methods=["POST"])
@keep_alive._form_door
def api_schedule_message():
    body = keep_alive._json_or_form()
    LAST_SCHEDULE.clear()
    LAST_SCHEDULE.update(body)
    now = int(time.time())
    run_at, repeat, error = keep_alive._schedule_timing(body, now)
    if error:
        return jsonify({"success": False, "error": error, "server_now": now}), 400
    # The real route's own order and own sentences: timing, then the channels,
    # then the content. This route used to hand-roll its checks, which meant a
    # body the bot refuses could still be *queued* here and reported as scheduled.
    channel_ids = keep_alive._parse_channel_ids(body)
    if not channel_ids:
        return jsonify({"success": False, "error": "Pick at least one channel.", "server_now": now}), 400
    if len(channel_ids) > keep_alive.MESSAGE_MAX_CHANNELS:
        return jsonify({
            "success": False,
            "error": f"At most {keep_alive.MESSAGE_MAX_CHANNELS} channels per send.",
            "server_now": now,
        }), 400
    by_id = {c["id"]: c for c in CHANNELS}
    if any(str(cid) not in by_id for cid in channel_ids):
        return jsonify({
            "success": False,
            "error": "That channel no longer exists — reload the page and pick one again.",
            "server_now": now,
        }), 400

    generator = str(body.get("generator") or "").strip()
    if generator and generator not in digest.GENERATORS:
        return jsonify({
            "success": False,
            "error": "Unknown content source — reload the page and pick one again.",
            "server_now": now,
        }), 400
    if generator:
        # A generated message has no text to validate — its content does not
        # exist until the moment it posts — so this must *not* run the message
        # builder, exactly as the real route does not. Running it here would
        # refuse the one kind of scheduled message that writes itself.
        payload = {
            "generator": generator,
            "content": None,
            "embed": None,
            "users": [],
            "roles": [],
            "everyone": False,
            "ignored_mentions": [],
        }
        preview = f"{digest.label(generator)} — built when it posts"
    else:
        # The same builder "send now" uses, so a queued message is validated
        # exactly as an immediate one is — including resolving every tag.
        payload, channel_ids, error = keep_alive._build_message(_PreviewGuild(), body)
        if error:
            return jsonify({"success": False, "error": error, "server_now": now}), 400
        preview = ""

    # The queue cap, which the real route enforces when it cannot create a row:
    # it answers 429 in these words. The harness reported ``max_pending`` to the
    # page and then accepted past it, so the preview let the queue read "32 of 25"
    # for a message the bot would have refused to queue at all.
    pending = sum(1 for r in SCHEDULE if r["status"] == "pending")
    if pending >= keep_alive.scheduled.MAX_PENDING_PER_GUILD:
        return jsonify({
            "success": False,
            "error": (
                f"This server already has {keep_alive.scheduled.MAX_PENDING_PER_GUILD} "
                "scheduled messages — cancel one before adding another."
            ),
            "server_now": now,
            "pending": pending,
            "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
            "catchup_hours": keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        }), 429

    content = str(payload.get("content") or "")
    row = {
        "id": (max((r["id"] for r in SCHEDULE), default=0) + 1),
        "channel_ids": [str(cid) for cid in channel_ids],
        "payload": payload,
        "preview": preview or " ".join(content.split())[:140],
        "run_at": run_at,
        "repeat_minutes": repeat,
        "status": "pending",
        "created_by_name": "RAJ",
        "last_run_at": None,
        "last_error": "",
        "results": [],
        "fire_count": 0,
    }
    SCHEDULE.append(row)
    return jsonify({
        "success": True,
        "server_now": now,
        "message": _schedule_view(row),
        "ignored_mentions": row["payload"]["ignored_mentions"],
        "pending": sum(1 for r in SCHEDULE if r["status"] == "pending"),
        "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
    })


def _find(msg_id):
    return next((r for r in SCHEDULE if r["id"] == msg_id), None)


@app.route("/api/scheduled/<int:msg_id>/cancel", methods=["POST"])
@keep_alive._form_door
def api_cancel_scheduled(msg_id: int):
    row = _find(msg_id)
    if row is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404
    if row["status"] != "pending":
        return jsonify({"success": False, "error": f"That message is already {row['status']}."}), 409
    row["status"] = "cancelled"
    row["last_run_at"] = int(time.time())
    row["last_error"] = "cancelled from the dashboard"
    return jsonify({"success": True, "id": msg_id, "server_now": int(time.time())})


@app.route("/api/scheduled/<int:msg_id>/fire", methods=["POST"])
@keep_alive._form_door
def api_fire_scheduled(msg_id: int):
    row = _find(msg_id)
    if row is None:
        return jsonify({"success": False, "error": "That is not a scheduled message in this server."}), 404
    if row["status"] != "pending":
        return jsonify({"success": False, "error": f"That message is already {row['status']}."}), 409
    by_id = {c["id"]: c for c in CHANNELS}
    now = int(time.time())
    sent, failed = [], []
    results = []
    for cid in row["channel_ids"]:
        channel = by_id.get(str(cid))
        if channel is None or not channel["can_send"]:
            reason = "That channel no longer exists" if channel is None else (
                f"I cannot send messages in #{channel['name']}"
            )
            failed.append({"channel_id": str(cid), "name": channel["name"] if channel else None,
                           "error": reason})
            results.append({"channel_id": str(cid), "name": channel["name"] if channel else None,
                            "ok": False, "error": reason, "jump_url": ""})
        else:
            url = f"https://discord.com/channels/777001/{cid}/1"
            sent.append({"channel_id": str(cid), "name": channel["name"], "jump_url": url})
            results.append({"channel_id": str(cid), "name": channel["name"], "ok": True,
                            "error": "", "jump_url": url})
    row["status"] = "sent" if not row["repeat_minutes"] else "pending"
    row["last_run_at"] = now
    row["fire_count"] += 1
    row["results"] = results
    row["last_error"] = "" if not failed else f"{len(failed)} channel(s) refused it: {failed[0]['error']}"
    if row["repeat_minutes"]:
        row["run_at"] = row["run_at"] + row["repeat_minutes"] * 60
        while row["run_at"] <= now:
            row["run_at"] += row["repeat_minutes"] * 60
    return jsonify({
        "success": bool(sent),
        "error": None if sent else (failed[0]["error"] if failed else "Nothing was sent."),
        "results": results,
        "sent": sent,
        "failed": failed,
        "ignored_mentions": row["payload"].get("ignored_mentions", []),
        "server_now": now,
    })


@app.route("/__last-send")
def last_send():
    """The ids the browser actually posted, verbatim."""
    return jsonify(LAST_SEND)


@app.route("/__last-schedule")
def last_schedule():
    """The scheduling body the browser actually posted, verbatim."""
    return jsonify(LAST_SCHEDULE)


@app.route("/__queue")
def queue_state():
    return jsonify(SCHEDULE)


def _preview_bot_loop(coro, timeout: float = 20.0):
    """Run one Discord call in this thread, because this process has no loop.

    ``keep_alive._on_bot_loop`` schedules the call on the bot's event loop and
    answers *the bot is not connected right now* when there is none — the honest
    sentence for a deployment whose bot is still booting, and the wrong one here,
    where there is no loop at all. That case has its own documented fallback in
    ``keep_alive._run_on_bot_loop``: no loop means no second, disagreeing view of
    a cache, so the call can simply run. The support bridge is the one write in
    this harness that *has* to reach the gateway, so this is where the rule is
    applied; without it the card's own form could only ever show the refusal.
    """
    result = asyncio.run(coro)
    return result if isinstance(result, tuple) else (result, 200)


if __name__ == "__main__":
    prepare_environment()
    # The gateway the console's own views read, and a small economy to look at.
    # Both only when *serving*: this file is imported by the test-suite, and
    # neither belongs to a test that never asked for one.
    keep_alive.BOT_INSTANCE = _PreviewBot()
    keep_alive._on_bot_loop = _preview_bot_loop
    seeded = seed_economy()
    counted = seed_activity()
    signed_in = seed_sessions()
    asked = seed_questions()
    print(
        f"[PREVIEW] gateway: {len(GUILDS)} servers; economy: {seeded} seeded row(s); "
        f"activity: {counted} server(s); sessions: {signed_in}; questions: {asked}",
        flush=True,
    )
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
