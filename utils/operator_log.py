"""The two logs the operator keeps about the bot itself.

**Who has the bot.** Every server it is added to, with who added it and how big
the server is, and every server it is taken out of — because "are people still
using this?" is otherwise a question nobody can answer from inside Discord.

**What people run.** Every command, with the member who ran it, the server and
the channel. It is how a feature nobody uses gets noticed, and how a broken
command gets reported before anyone complains.

Both go to the **operator's own server** — the channels are the ones the bot
binds there by name (``#active-servers``, ``#real-time-cmd-data``) through
:mod:`utils.bot_channels` — so a member's own server never has to know.

Four rules, three of them learned from a failure this project already had:

* **Never break the thing being logged.** A log write that raises would turn a
  working command into a broken one, so every entry here is best-effort and
  reports its outcome instead of propagating anything.
* **Never ping.** These cards name members and servers; a card suppresses its
  mentions by default (:data:`utils.style.NO_PINGS`) and the names are plain text,
  so a log entry cannot notify a server.
* **Never invent.** An entry that cannot be written is reported rather than
  dropped in silence, and a value the bot does not know reads "unknown" rather
  than a guess.
* **Say when it is not working.** The host log is not an audience. A feature that
  is installed and writes nowhere looks *identical* to one that is working, and
  the only symptom used to be a line in a container nobody reads — so the first
  entry with nowhere to go now posts a card where the operator already looks,
  naming the channel to create (:func:`_notice_missing`), and the boot that wires
  these channels up says what they are for (:func:`announce_setup`).
* **A command that breaks is written down too** (:func:`command_failed`).
  Completion only fires for a command that worked, so the log used to be silent
  for exactly the entries that matter most — and an empty ``#real-time-cmd-data``
  then reads as "nobody uses this bot" instead of "this bot is broken".
* **One copy of this bot answers.** Discord delivers an interaction to every
  connected session of the same application, so a second copy — a laptop running
  ``bot.py`` while the deploy is up — races the first one: one of them answers,
  the loser raises *Interaction has already been acknowledged*, and the Discord
  user sees "the application did not respond". The boot that finds another live
  copy says so where the operator looks (:func:`report_second_instance`).

* **A check that could not be made is reported too**
  (:func:`site_guard_alert`). The bot also asks, on a timer, whether the public
  site is serving the repository (see :mod:`utils.site_guard`). That fault has no
  symptom inside Discord — the pages look right, the bot stays connected, the
  deploy reports success — so the alert is the whole detection, and a check that
  could not reach the site is reported as loudly as one that found a leak.

What is deliberately *not* recorded: the values typed into the command. Those are
whatever a person chose to write — a `/bug` report, a `/pay` note, a `/purge`
reason — and copying them into a second server is a privacy change, not a logging
detail. The command's name, its runner and its outcome are what the log is for.
"""

import discord

from utils import bot_channels, site_guard, style


async def _channel(bot, role: str):
    """The operator's channel for ``role``, or ``None`` if there is not one.

    Resolved through the same bindings every other house channel uses, so moving
    the log is a rename or a dashboard setting — not an edit here.
    """
    guild = bot.get_guild(bot_channels.bot_server_guild_id())
    if guild is None:
        return None
    channel_id = await bot_channels.bound(guild.id, role)
    if channel_id is None:
        return None
    return bot.get_channel(channel_id)


#: What each operator channel receives, in the words the setup card uses.
_WHAT_ARRIVES: dict[str, str] = {
    "guilds": "one card each time a server **adds** the bot or **removes** it",
    "cmdlog": "one card for **every command**, with who ran it and where",
}

#: Where a "nowhere to write" notice goes, most-watched first. The update channel
#: is the one an operator already reads after every deploy; the error channel is
#: where a fault goes; the log channel is the last place anything lands.
_NOTICE_ROLES: tuple[str, ...] = ("update", "error", "log")

#: Roles that have already reported having nowhere to write. A notice that
#: repeats on every command would bury the very channel it is trying to save.
_warned: set[str] = set()

#: Where a "two copies of me are running" card goes, best fit first: a duplicated
#: bot is a fault about the bot itself, so the error channel comes first here even
#: though the update channel is the one read most.
_INSTANCE_ROLES: tuple[str, ...] = ("error", "update", "log")


async def _notice_missing(bot, role: str, kind: str) -> None:
    """Say once, where the operator will actually see it, that an entry was lost.

    This is the difference between a feature that is *installed* and one that is
    *working*: with nothing bound, both look like an empty channel, and the only
    evidence used to be a print into the host's log. The card names the channel
    to create and the way to bind it without a restart.

    Marked as warned before the attempt, so a notice that cannot be posted does
    not try again on every command. Best-effort like everything else here.
    """
    if role in _warned:
        return
    _warned.add(role)

    print(
        f"[LOG] no {role} channel bound on the bot's own server — {kind} was not "
        "written to Discord",
        flush=True,
    )

    target = None
    for notice_role in _NOTICE_ROLES:
        target = await _channel(bot, notice_role)
        if target is not None:
            break
    if target is None:
        # Nothing is bound anywhere, so the host log is the only place left. Said
        # once per role, which is what keeps this from being a loop.
        print(
            "[LOG] nothing is bound on the bot's own server to report that in",
            flush=True,
        )
        return

    wanted = bot_channels.candidates(role)
    embed = discord.Embed(
        title="⚠️ Nothing is bound to receive this",
        description=(
            "An entry of mine had nowhere to go, so it was **dropped**. "
            "This card is sent once per channel, not once per entry."
        ),
        color=style.WARN,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Was writing", value=f"`{kind}`", inline=False)
    embed.add_field(
        name="Meant for",
        value=f"`{role}` — {_WHAT_ARRIVES.get(role, 'a log entry')}",
        inline=False,
    )
    if wanted:
        embed.add_field(
            name="Fix it",
            value=(
                f"Create `#{wanted[0]}` in this server — it binds by name on the "
                "next boot — or run `/bot db` here to bind it now."
            ),
            inline=False,
        )
    embed.set_footer(text="Meow · server log")
    try:
        await style.send_card(target, embed=embed, timeout=None)
    except Exception as exc:
        print(f"[LOG] could not even report the missing {role} channel: {exc}", flush=True)


async def report_second_instance(bot, owner: str) -> bool:
    """Say that another copy of this bot is connected and answering.

    This is the one fault that is invisible from inside Discord. Two sessions of
    one application both receive every interaction, so each command is handled
    twice: whichever copy answers first wins, the other raises *Interaction has
    already been acknowledged*, and any interaction delivered to a session that
    has since gone quiet is answered too late to count — which is what a member
    sees as "the application did not respond". Nothing in either copy's host log
    names the cause, and the damage looks like a broken bot rather than a
    duplicated one.

    ``owner`` is the other copy's own name for itself, from the database lease in
    :mod:`utils.cloud_sync` — the same lease that already stops the two copies
    fighting over the backup, read here for the other half of the same problem.
    """
    target = None
    for role in _INSTANCE_ROLES:
        target = await _channel(bot, role)
        if target is not None:
            break
    if target is None:
        return False

    embed = discord.Embed(
        title="🚨 Another copy of me is online",
        description=(
            "Two sessions are connected with this bot's token, so **every command "
            "is delivered to both of them**. One answers; the other fails — and "
            "the commands that fail look like *The application did not respond* "
            "in the channel they were run in."
        ),
        color=style.DANGER,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="The copy that was already serving", value=f"`{owner}`", inline=False)
    embed.add_field(
        name="Which one to stop",
        value=(
            f"**This** one. `{owner}` is holding the backup and is already "
            "serving — the copy that reads this card is the second one to arrive. "
            "Nothing needs deleting in the backup bucket: that claim is refreshed "
            "every two minutes and disappears on its own once one copy stops."
        ),
        inline=False,
    )
    # Not "reported once": this fires on every boot that finds the other copy,
    # which is the cadence that keeps a forgotten duplicate from being forgotten
    # again — and a boot is rare enough that it cannot become noise.
    embed.set_footer(text="Meow · reported by the copy that found it")
    try:
        await style.send_card(target, embed=embed, timeout=None)
        return True
    except Exception as exc:
        print(f"[LOG] could not report the second instance: {exc}", flush=True)
        return False


#: The four cards this check can send, keyed by the verdict it is answering:
#: ``status -> (title, what the page says, colour, what to do about it)``.
#:
#: Kept together because the pairings are the whole content of the alert, and
#: because two of them are *not* a leak. Telling an operator that a leak has
#: recurred when the deployment is clean and only a CDN copy is left over — the
#: mistake the first live run of the check made — sends them to act on the build
#: settings, which are correct, while the copy keeps being served. Each row
#: therefore names its own remedy, and only one of them says *purge the cache*.
_SITE_GUARD_CARDS: dict[str, tuple[str, str, int, str]] = {
    "exposed": (
        "🚨 The website is serving the repository",
        "Paths that belong to the source tree are **readable by anyone** at the "
        "site's own address. Nothing on the site looks wrong — this is not a "
        "broken page, it is the checkout being published as one.",
        style.DANGER,
        "The Cloudflare Pages build settings: the build command and the **build "
        "output directory** must both be set, and it is the output directory "
        "that stops a static host being handed the checkout. "
        "`docs/deployment.md` has both, and `scripts/build_site.py` refuses to "
        "build into the repository root.",
    ),
    "cached": (
        "⚠️ A cached copy of the repository is still being served",
        "The site itself refuses these paths, so the deployment is **not** "
        "publishing the tree — but a copy left by the deploy that was, is still "
        "being handed out at the site's address.",
        style.WARN,
        "**Not** the build settings — they are correct, and re-deploying does "
        "not clear this: Cloudflare deliberately keeps a previous deployment's "
        "assets served for a while so a rollout cannot break an app mid-visit, "
        "and `Purge Everything` does not reach a `*.pages.dev` host. It expires "
        "on its own, and this check goes quiet when it does. To make it go "
        "sooner, Cloudflare support is the only party that can — they own that "
        "zone.",
    ),
    "fallback": (
        "⚠️ The site answers everything, so the check cannot tell",
        "A path that does not exist answered with a page, which means this host "
        "serves *something* for any address. A `200` on `/bot.py` therefore "
        "says nothing about `/bot.py`, and whether the tree is public is "
        "**unknown** rather than clean.",
        style.WARN,
        "A single-page-app fallback or a catch-all route is answering for "
        "missing paths. This is not a leak by itself — but until it is removed "
        "the site cannot be checked this way, and it hides a real one.",
    ),
    "unreachable": (
        "⚠️ Could not check what the website is serving",
        "The check ran and the site did not answer, so whether the source tree "
        "is public was **not** established either way. This is reported rather "
        "than ignored: a guard that has gone blind looks exactly like a guard "
        "that found nothing.",
        style.WARN,
        "Nothing unless it repeats — a host that is asleep answers like this. "
        "If it keeps answering this way while the site is up in a browser, "
        "`SITE_URL` names the wrong address.",
    ),
}


async def site_guard_alert(bot, verdict) -> bool:
    """Say what the public site is serving, when it is not simply *nothing*.

    This is the card for the one incident that leaves no trace in the product.
    When the Pages project published the checkout on 2026-09-19, every page
    looked correct, the bot stayed connected, and the deploy reported success;
    the only evidence was a line in a build log. So the card does not describe a
    symptom, because there is not one — it names what answered and what to do.

    Four states share this entry point because they are one question asked
    under different conditions, and the operator needs the *difference*: a
    **DANGER** card for a site publishing the tree, and a **WARN** card for a
    stale CDN copy, a host that answers everything, or a site that could not be
    reached. The last three matter as much as the first — the first live run of
    this check reported a live leak that was only a cached copy, and a guard
    that has gone blind is indistinguishable from a guard that found nothing,
    which is how the original exposure stayed invisible.
    :func:`utils.site_guard.note` decides which of them is worth saying right
    now, and this just delivers it.
    """
    card = _SITE_GUARD_CARDS.get(verdict.status)
    if card is None:  # `clean` never gets here; `note` does not return for it.
        return False
    title, description, colour, remedy = card
    target = None
    for role in _NOTICE_ROLES:
        target = await _channel(bot, role)
        if target is not None:
            break
    if target is None:
        return False

    embed = discord.Embed(
        title=title,
        description=description,
        color=colour,
        timestamp=discord.utils.utcnow(),
    )

    # What was actually found, in the shape that finding has. Only `exposed` and
    # `cached` have paths to name; the other two are about the check, not a file.
    if verdict.status in ("exposed", "cached"):
        paths = verdict.exposed if verdict.status == "exposed" else verdict.cached_paths
        embed.add_field(
            name="Public right now" if verdict.status == "exposed" else "Still served from cache",
            value="\n".join(f"`/{path}`" for path in paths)[:1024],
            inline=False,
        )
    elif verdict.status == "fallback":
        embed.add_field(
            name="A path that does not exist answered",
            value=f"`HTTP {verdict.control}` for `/{site_guard.CONTROL_PREFIX}-…`",
            inline=False,
        )
    else:
        embed.add_field(
            name="No answer for",
            value="\n".join(f"`/{path}`" for path in verdict.unanswered)[:1024]
            or "every probe",
            inline=False,
        )

    embed.add_field(name="What to do", value=remedy, inline=False)
    # The footer says *how* the site was asked, and it has to agree with the card
    # above it: an exposure was established by going around the cache, a cached
    # copy was found by going through it as well. Getting this backwards would
    # make the reader doubt the card they are looking at.
    how = (
        "without its cache" if verdict.status == "exposed"
        else "with and without its cache" if verdict.status == "cached"
        else ""
    )
    embed.set_footer(
        text=f"Meow · asked {verdict.site}{' ' + how if how else ''} · {len(verdict.answers)} paths"
    )
    try:
        await style.send_card(target, embed=embed, timeout=None)
        return True
    except Exception as exc:
        print(f"[LOG] could not report the site check: {exc}", flush=True)
        return False


async def uptime_alert(bot, snapshot, message: str) -> bool:
    """Say that a monitor changed state, with the evidence beside it.

    This is the half of :mod:`utils.uptime_status` that has an audience. The
    history itself is read wherever somebody looks for it (the dashboard's
    status panel, `/health`), but a *change* is only useful if it arrives
    without anybody looking — and this is the one place the bot already talks to
    the operator.

    It is honest about its own limit, which is worth saying because the monitor
    most likely to matter watches this very host: a process that is down cannot
    announce that it is down, so this card reports what UptimeRobot saw, and a
    monitor **of this host** is the case where UptimeRobot's own notification is
    the only one that can arrive.
    """
    down = [row for row in snapshot.get("monitors") or [] if row.get("down")]
    embed = discord.Embed(
        title="📉 UptimeRobot reports a monitor down" if down else "📈 UptimeRobot reports a recovery",
        description=message,
        color=style.DANGER if down else style.SUCCESS,
        timestamp=discord.utils.utcnow(),
    )
    if down:
        embed.add_field(
            name="Down right now",
            value="\n".join(f"`{row['name']}` — {row['label']}" for row in down)[:1024],
            inline=False,
        )
    rows = [
        f"**{row['name']}** — {row['label']}"
        + (
            f", {(row.get('ratios') or {}).get('30d'):.2f}% over 30 days"
            if (row.get("ratios") or {}).get("30d") is not None
            else ""
        )
        for row in (snapshot.get("monitors") or [])[:10]
    ]
    embed.add_field(
        name=f"Every monitor ({snapshot.get('total', 0)})",
        value="\n".join(rows)[:1024] or "none configured",
        inline=False,
    )
    embed.add_field(
        name="What to do",
        value=(
            "UptimeRobot holds the check history and its own notification "
            "settings; this card is the bot telling you what it read. A monitor "
            "watching **this host** cannot be announced by this host — for that "
            "one, UptimeRobot's own alert is what reaches you."
            if down
            else "Nothing to do. This is the recovery, sent because the last card said down."
        ),
        inline=False,
    )
    weakest = snapshot.get("ratio_30d")
    if weakest is not None:
        embed.set_footer(text=f"Weakest 30-day uptime across monitors: {weakest:.2f}%")
    target = None
    for role in _INSTANCE_ROLES:
        target = await _channel(bot, role)
        if target is not None:
            break
    if target is None:
        return False
    try:
        await style.send_card(target, embed=embed, timeout=None)
        return True
    except Exception as exc:
        print(f"[LOG] could not report the uptime change: {exc}", flush=True)
        return False


async def _post(bot, role: str, kind: str, embed: discord.Embed) -> bool:
    """Send one entry, and never raise: the caller is a command that just worked."""
    try:
        channel = await _channel(bot, role)
        if channel is None:
            await _notice_missing(bot, role, kind)
            return False
        await style.send_card(channel, embed=embed, timeout=None)
        return True
    except Exception as exc:  # a log must not be able to break a command
        print(f"[LOG] could not write the {kind} entry: {exc}", flush=True)
        return False


def _server_value(guild) -> str:
    """A server as a name and an id: greppable, and never a mention."""
    name = getattr(guild, "name", None) or "unknown"
    guild_id = getattr(guild, "id", None)
    return f"**{name}**\n`{guild_id}`" if guild_id else f"**{name}**"


def _member_value(user) -> str:
    """A member the same way: a plain display name, then their id."""
    if user is None:
        return "unknown"
    name = getattr(user, "display_name", None) or getattr(user, "name", None) or "unknown"
    user_id = getattr(user, "id", None)
    return f"**{name}**\n`{user_id}`" if user_id else f"**{name}**"


def _count(guild, attribute: str) -> str:
    value = getattr(guild, attribute, None)
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "unknown"


async def _added_by(guild) -> str:
    """Who added the bot, read from the server's audit log when we may.

    Reading it needs View Audit Log, which a fresh invite may not have granted
    yet — so "unknown" is a real answer here rather than a failure, and the field
    says which permission would fill it in.
    """
    me = getattr(guild, "me", None)
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            target = getattr(entry, "target", None)
            if target is None or me is None or getattr(target, "id", None) != getattr(me, "id", None):
                continue
            return _member_value(getattr(entry, "user", None))
    except Exception as exc:
        print(f"[LOG] could not read the audit log for {getattr(guild, 'name', '?')}: {exc}", flush=True)
    return "_unknown — needs View Audit Log_"


async def guild_joined(bot, guild) -> bool:
    """A server added the bot."""
    embed = discord.Embed(
        title="➕ Added to a server",
        color=style.SUCCESS,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Server", value=_server_value(guild), inline=True)
    embed.add_field(name="Members", value=_count(guild, "member_count"), inline=True)
    embed.add_field(name="Owner", value=_member_value(getattr(guild, "owner", None)), inline=True)
    embed.add_field(name="Added by", value=await _added_by(guild), inline=True)
    embed.add_field(name="Servers now", value=str(len(getattr(bot, "guilds", []))), inline=True)
    embed.set_footer(text="Meow · server log")
    return await _post(bot, "guilds", f"join of {getattr(guild, 'name', '?')}", embed)


async def guild_left(bot, guild) -> bool:
    """A server removed the bot (or was deleted, or the bot was banned)."""
    embed = discord.Embed(
        title="➖ Removed from a server",
        color=style.DANGER,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Server", value=_server_value(guild), inline=True)
    embed.add_field(name="Members", value=_count(guild, "member_count"), inline=True)
    embed.add_field(name="Servers left", value=str(len(getattr(bot, "guilds", []))), inline=True)
    embed.add_field(name="Was there since", value=_joined_at(guild), inline=True)
    # Discord reports a kick, a deletion and a ban as one event, so the card says
    # that rather than letting the reader assume which one it was.
    embed.add_field(
        name="Why",
        value="kicked, deleted, or banned — Discord reports these as one event",
        inline=False,
    )
    embed.set_footer(text="Meow · server log")
    return await _post(bot, "guilds", f"leaving {getattr(guild, 'name', '?')}", embed)


async def announce_setup(bot, guild, newly) -> bool:
    """The one-time card that says what these two channels are for.

    Called when a boot binds them for the first time, which is the only moment a
    server has just *created* a channel whose purpose nothing on the page states —
    and the same card lands in the channel itself, so an empty log explains the
    silence instead of looking like a bot that was never installed.

    ``newly`` is what :func:`utils.bot_channels.ensure_bound` just bound, so a
    boot that changes nothing says nothing: this appears once, and a third
    channel added here years later gets its own card.

    Total, like every other entry point here: the whole body is guarded because
    its caller is a boot loop that has other servers left to bind, and a fuzz
    over deliberately broken clients caught this one raising where its siblings
    returned ``False``.
    """
    roles = [role for role in _WHAT_ARRIVES if role in (newly or {})]
    if not roles:
        return False
    try:
        return await _announce_setup(bot, guild, newly, roles)
    except Exception as exc:
        print(f"[LOG] could not announce the operator channels: {exc}", flush=True)
        return False


async def _announce_setup(bot, guild, newly, roles) -> bool:
    """Build and post the setup card. Guarded by :func:`announce_setup`."""
    # Into the channel that was just bound; the join/leave log is the one that
    # reads as "what is this?" when it is empty.
    target_role = "guilds" if "guilds" in roles else roles[0]
    # ``newly`` is what this boot wrote, so it is believed before the store is
    # read back: the card is the whole point of the bind, and it must not be lost
    # to a write that has not landed (or did not).
    target_id = (newly or {}).get(target_role)
    target = bot.get_channel(target_id) if target_id is not None else None
    if target is None:
        target = await _channel(bot, target_role)
    if target is None:
        return False

    embed = discord.Embed(
        title="📊 The bot's own log, wired up",
        description=(
            "These two channels are mine — the only place I write about *myself* "
            "rather than about a server I am in. Nothing from a member's server is "
            "copied here beyond a name, an id and a count."
        ),
        color=style.ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    for role, arrives in _WHAT_ARRIVES.items():
        channel_id = (newly or {}).get(role)
        if channel_id is None:
            channel_id = await bot_channels.bound(guild.id, role)
        if channel_id is None:  # noqa: SIM114 - the two branches say different things
            wanted = bot_channels.candidates(role)
            name = f"#{wanted[0]}" if wanted else role
            embed.add_field(
                name=role,
                value=f"⚠️ not bound — create `{name}` and it is picked up",
                inline=True,
            )
            continue
        channel = bot.get_channel(channel_id)
        label = f"#{getattr(channel, 'name', channel_id)}" if channel is not None else str(channel_id)
        embed.add_field(name=role, value=f"**{label}** — {arrives}", inline=True)
    embed.add_field(
        name="Rename anything?",
        value=(
            "These bind by *name*, once. Renaming the channel later moves nothing — "
            "the binding is remembered, so run `/bot db` to re-bind it here."
        ),
        inline=False,
    )
    embed.set_footer(text="Meow · appears once, when these channels are first bound")
    await style.send_card(target, embed=embed, timeout=None)
    return True


def _joined_at(guild) -> str:
    """When the bot joined, as Discord's own timestamp markup — or unknown."""
    me = getattr(guild, "me", None)
    joined = getattr(me, "joined_at", None) if me is not None else None
    if joined is None:
        return "unknown"
    return f"<t:{int(joined.timestamp())}:f> (<t:{int(joined.timestamp())}:R>)"


#: How much of an error's own words the log carries. Discord's errors are one
#: line; this is a ceiling, not a target.
_REASON_LIMIT: int = 300


def _reason(error) -> str:
    """What went wrong, named — the one field a failure entry is worth having for.

    ``CommandInvokeError`` is discord.py's wrapper around whatever the command
    actually raised, and the wrapper's own text is the noisy one, so it is
    unwrapped first.

    Only a *Discord* error carries its message. A Python-level failure is
    written down as its type alone: an exception raised while handling what
    somebody typed can quote it back, and this log promises never to copy the
    values typed into a command. The full traceback goes to the error channel
    through ``bot.py``'s report, which is where a message may legitimately live.
    """
    original = getattr(error, "original", None)
    cause = original if isinstance(original, BaseException) else error
    name = type(cause).__name__
    if not isinstance(cause, discord.DiscordException):
        return f"`{name}`"
    detail = " ".join(str(cause).split())
    return f"`{name}` — {detail[:_REASON_LIMIT]}" if detail else f"`{name}`"


async def command_failed(bot, interaction: discord.Interaction, error) -> bool:
    """A command that ended in an error, in the channel the successes go to.

    A second card rather than an ``outcome`` field on :func:`command_used`, and
    not because of taste: ``on_app_command_completion`` is dispatched only in the
    ``else`` branch of discord.py's own call, so a command that raised never
    reaches that function at all. Before this existed, a broken command wrote
    nothing anywhere the operator reads.

    The interaction is never touched. A command whose *answer* already failed —
    an expired interaction, a second copy of the bot that got there first — still
    has a name, a runner and a reason worth writing down, and a channel send is
    not an interaction response. That is the case this entry exists for.
    """
    guild = getattr(interaction, "guild", None)
    channel = getattr(interaction, "channel", None)
    command = getattr(interaction, "command", None)
    name = getattr(command, "qualified_name", None) or "unknown"
    # The command's own name goes in the author line — the same place the
    # success card puts it — so the two entries read as a pair in the channel.
    embed = discord.Embed(
        title="❌ Command failed",
        color=style.DANGER,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Reason", value=_reason(error), inline=False)
    embed.add_field(
        name="User",
        value=_member_value(getattr(interaction, "user", None)),
        inline=True,
    )
    embed.add_field(
        name="Server",
        value=_server_value(guild) if guild is not None else "**Direct message**",
        inline=True,
    )
    embed.add_field(
        name="Channel",
        value=f"#{getattr(channel, 'name', 'unknown')}" if channel is not None else "—",
        inline=True,
    )
    embed.set_author(name="/" + str(name).split(" ")[0])
    embed.set_footer(text="Meow · command log")
    return await _post(bot, "cmdlog", f"failed /{name}", embed)


async def command_used(bot, interaction: discord.Interaction, command) -> bool:
    """One command, with who ran it and where — never what they typed."""
    guild = getattr(interaction, "guild", None)
    channel = getattr(interaction, "channel", None)
    embed = discord.Embed(
        description=f"## /{getattr(command, 'qualified_name', 'unknown')}",
        color=style.ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=_member_value(getattr(interaction, "user", None)), inline=True)
    embed.add_field(
        name="Server",
        value=_server_value(guild) if guild is not None else "**Direct message**",
        inline=True,
    )
    embed.add_field(
        name="Channel",
        value=f"#{getattr(channel, 'name', 'unknown')}" if channel is not None else "—",
        inline=True,
    )
    embed.set_author(name="/" + str(getattr(command, "qualified_name", "unknown")).split(" ")[0])
    embed.set_footer(text="Meow · command log")
    return await _post(bot, "cmdlog", f"/{getattr(command, 'qualified_name', '?')}", embed)
