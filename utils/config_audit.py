"""Every setting this deployment can be given, and what its absence costs.

The problem this exists to solve is not that the bot reads the environment. It is
that **nothing knew the whole set.** Forty-odd ``os.getenv`` calls are spread
across the tree, each with its own fallback — a blank string here, a committed
default there, ``0`` for a user id — and each one decides on its own whether to
say anything when it is empty. A handful print a warning; most say nothing.

The symptom is always the same shape, and it has cost this project real time,
repeatedly: **a feature that is configured-and-off is indistinguishable from a
feature that is broken.** Three examples, all from this repository:

1. ``DISCORD_CLIENT_ID`` was declared in ``render.yaml`` and absent from the
   running service. The landing page drops its invite button when the id is
   unknown, so the live site had no way to add the bot — and nothing, anywhere,
   said why. The one clue was a template silently rendering one element fewer.
2. The site's own deploy stopped publishing for four commits while reporting
   success. Every guard was about the build, the build was fine, and what was
   missing was a credential.
3. ``load_dotenv()`` was called *after* ``keep_alive`` and ``utils`` had been
   imported. Their module-level reads had already happened, so a ``.env`` — the
   documented way to configure this locally — reached ``DISCORD_TOKEN`` and
   nothing else: the dashboard ran on a throwaway session key with passkey
   sign-in switched off, and backups were disabled, on a machine where all three
   were filled in.

None of those was a code defect in the usual sense. Each was *invisible
configuration*, and no amount of careful reading would have found any of them,
because a reader has to hold the whole set in mind to notice what is absent from
it. That is what ``SETTINGS`` is for.

Three consumers, chosen because they are surfaces people already look at:

* **the boot log** — ``boot_lines()`` — so a host's log opens by naming what is
  off, where an operator is already reading;
* **``/health``** — ``health_json()`` — so a monitor, or one ``curl``, can see
  that something is unset. It counts, and deliberately does not name: ``/health``
  is public and unauthenticated, and "this deployment has no sign-in secret" is
  not a sentence to hand a stranger;
* **``/api/config``** — ``full_json()`` — behind the administrator's sign-in,
  where the names, the purposes and the costs are.

It also **reads** the environment, and it is the only place in the running
application that does. ``get``/``flag`` are the two functions every runtime
module calls, so "what can this deployment be configured with" has a single
answer: the tuple below. That rule is enforced rather than trusted —
``tests/test_config_audit.py`` walks the source and fails if ``keep_alive``,
``bot.py``, ``cogs/`` or ``utils/`` reaches for ``os.environ`` directly. The
build tooling under ``scripts/`` is deliberately exempt: it runs on a different
machine, and ``tests/test_config_audit.py`` still proves every name it reads is
declared here.

The registry is also *checked* in both directions: a setting read but not
declared fails a test, and so does one declared here that nothing reads. It found
both an omission in the first draft of this file (``DISCORD_CLIENT_ID``, of all
things) and a setting nothing read, which is the point of writing the guard
before trusting the list.

And it never reports a **value**, only whether one is there. Half of these are
credentials — a bot token, a Supabase service key that is the database's password,
a passkey — so the report carries names, booleans and prose, and ``secret`` marks
the ones whose value must never reach a log, a page or Discord.

Where a missing setting has a real consequence *at a specific moment*, the module
that hits it still prints its own warning — ``cloud_sync`` when it skips a
restore, ``keep_alive`` when it invents a session key. Those stay: they fire with
the context of the thing that just failed. This module answers the different
question, which is "what is this deployment **not** doing?", and it answers it
once, at boot, over the whole set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# What kind of loss an absence is
# --------------------------------------------------------------------------- #
#: The process cannot do its job at all. Every other setting is optional next to
#: these.
ESSENTIAL = "essential"
#: It runs, and a protection is weaker than it looks. The dangerous class: the
#: deployment looks healthy and is not as safe as the configuration implies.
SECURITY = "security"
#: A whole feature is quietly off. The class this module exists for — nobody
#: notices, because there is no error, only something that is not there.
CAPABILITY = "capability"
#: Absence is the normal, intended state: the code has a working default, or the
#: switch ships off on purpose. Reported for completeness, never as a problem.
TUNING = "tuning"
#: Supplied by the platform rather than by a person. Listed so the parity test can
#: account for every name, and never reported as missing.
HOST = "host"

#: Worst first. The order the report is read in.
SEVERITY = (ESSENTIAL, SECURITY, CAPABILITY, TUNING, HOST)

#: What a person can act on: an absence here changes what the deployment does.
#: ``HOST`` is excluded because nobody sets it, and ``TUNING`` because its absence
#: is a decision the defaults already made.
NOTABLE = (ESSENTIAL, SECURITY, CAPABILITY)

#: The running bot and dashboard, versus the tooling that builds the public site.
#: Kept apart because the two are configured on different machines, and saying
#: "LIVE_APP_URL is not set" on a bot host is a true sentence about nothing.
RUNTIME = "runtime"
BUILD = "build"


@dataclass(frozen=True)
class Setting:
    """One environment variable: what it turns on, and what its absence costs.

    ``what`` is written for the page that lists things, ``cost`` for the moment
    somebody is deciding whether to go and set it. ``cost`` is the one that
    matters: it describes the *symptom* a person would otherwise be left to
    guess at, which is the entire reason they are reading this.

    ``implied_by`` is for settings the code can work out for itself. The host
    markers are the example: ``keep_alive`` reads ``DASHBOARD_HTTPS`` and
    ``TRUST_PROXY_HEADERS`` from the environment *or* infers both from ``RENDER``,
    so on Render those two are absent and correct at the same time. A report that
    shouts about them every boot is a report nobody reads by the third deploy —
    which is the failure this module was written to stop, not to reproduce. Name
    the settings that already cover it, and the absence is quiet when one of them
    is set, and loud when none of them is, which is the deployment that can
    actually be caught out by it.
    """

    name: str
    kind: str
    what: str
    cost: str
    secret: bool = False
    scope: str = RUNTIME
    #: Settings that make this one redundant when any of them is already set.
    implied_by: tuple[str, ...] = ()

    @property
    def notable(self) -> bool:
        """Whether an operator has something to decide when this is absent."""
        return self.kind in NOTABLE


# --------------------------------------------------------------------------- #
# The set
# --------------------------------------------------------------------------- #
#: The whole surface, in one tuple. Grouped by the part of the deployment it
#: belongs to so a reader can find things; rendered grouped by severity.
SETTINGS: tuple[Setting, ...] = (
    # --- getting the bot online -------------------------------------------- #
    Setting(
        "DISCORD_TOKEN",
        ESSENTIAL,
        "the bot's identity, and the credential it connects to Discord with",
        "the bot cannot log in at all. Nothing else in this list matters until "
        "this one is set.",
        secret=True,
    ),
    Setting(
        "ENABLE_MESSAGE_CONTENT_INTENT",
        CAPABILITY,
        "the Message Content intent, which Discord gates behind the Developer "
        "Portal as well",
        "message-length XP and AutoMod link filtering do nothing. AutoMod still "
        "deletes what it is told to, but its \u201clook back at recent messages\u201d "
        "and the XP-from-chat path see nothing. Set it in the portal *first*, "
        "then here \u2014 asking for an intent the portal has not enabled makes "
        "Discord refuse the entire connection.",
    ),
    Setting(
        "ENABLE_MEMBERS_INTENT",
        CAPABILITY,
        "the Server Members intent, which Discord gates behind the Developer "
        "Portal as well",
        "member join/leave events never arrive, so welcome/goodbye messages and "
        "anything counting members are silent. Same portal caveat as above.",
    ),
    Setting(
        "ENABLE_PRESENCE_INTENT",
        CAPABILITY,
        "the Presence intent, which is what makes \u201cwho is online\u201d answerable "
        "at all",
        "the owner console's Live page reports no presence figures \u2014 and reports "
        "them as *not read*, not as zero, because Discord answers \u201coffline\u201d for "
        "every member without it. Voice rosters, who is alone and who is streaming "
        "keep working: those come from voice states and need no privileged intent. "
        "Same portal caveat as above \u2014 enable Presence Intent in the portal, then "
        "set this.",
    ),

    # --- the invite link, and Discord sign-in ------------------------------ #
    Setting(
        "DISCORD_CLIENT_ID",
        CAPABILITY,
        "the application's client id, which the invite link and Discord sign-in "
        "are both built from",
        "the public pages render without an invite button \u2014 a template drops the "
        "button when the id is unknown \u2014 so the live site has no way to add the "
        "bot, and the login page cannot offer Discord either. This is the setting "
        "that was declared in render.yaml and absent from the running service.",
    ),
    Setting(
        "DISCORD_CLIENT_SECRET",
        CAPABILITY,
        "the half of Discord sign-in that completes the OAuth code exchange",
        "sign-in needs both halves, so /login never offers \u201cSign in with "
        "Discord\u201d, and a server's own administrators have to be handed the "
        "operator's passkey or a one-time link instead.",
        secret=True,
    ),

    # --- the dashboard's ways in ------------------------------------------- #
    Setting(
        "FLASK_SECRET_KEY",
        SECURITY,
        "the key dashboard sessions are signed with",
        "the app invents a new random key on every boot, so every restart \u2014 and "
        "every wake-up from a free-tier sleep \u2014 signs the administrator out and "
        "invalidates any one-time link they were holding.",
        secret=True,
    ),
    Setting(
        "ADMIN_PASSKEY",
        SECURITY,
        "sign-in with the operator's passkey, and the passkey-authenticated API "
        "endpoints",
        "passkey sign-in is disabled outright, and the only way in is the "
        "one-time link behind the \u26a1 Open Dashboard button on /help, which needs "
        "the bot itself awake to answer.",
        secret=True,
    ),
    Setting(
        "DASHBOARD_HTTPS",
        SECURITY,
        "that this host is reachable over HTTPS and nothing else",
        "inferred from RENDER/RAILWAY_ENVIRONMENT, so it is only needed behind "
        "your own TLS terminator. Unset on such a host, the session cookie is "
        "issued without its Secure flag and a plaintext request to the app's port "
        "would carry it.",
        implied_by=("RENDER", "RAILWAY_ENVIRONMENT"),
    ),
    Setting(
        "TRUST_PROXY_HEADERS",
        SECURITY,
        "that X-Forwarded-For may be believed when rate limiting a sign-in",
        "inferred on Render and Railway, and only needed behind your own reverse "
        "proxy. Unset on such a host, every request appears to come from the "
        "proxy's address \u2014 so the login rate limit counts everybody as one "
        "client, and one attacker can lock out everyone.",
        implied_by=("RENDER", "RAILWAY_ENVIRONMENT"),
    ),
    Setting(
        "DASHBOARD_BASE_URL",
        TUNING,
        "the absolute URL the dashboard is served from",
        "the compiled default is used, and the login page prints the callback URL "
        "it thinks Discord should be told. Wrong here means every Discord sign-in "
        "fails with a redirect_uri mismatch.",
    ),
    Setting(
        "OWNER_USER_ID",
        CAPABILITY,
        "which Discord user is the bot's owner, for the actions that are theirs "
        "alone to approve",
        "no user is the owner, so those approvals report that the owner has not "
        "been configured and refuse.",
    ),
    Setting(
        "OWNER_DISCORD_ELEVATION",
        SECURITY,
        "that a Discord sign-in matching OWNER_USER_ID may open the operator "
        "console on its own",
        "the passkey opens the console and nothing else does \u2014 which is the "
        "intent. Set it to 1 and the sign-in button becomes a complete sign-in for "
        "that Discord account, so whoever is holding the session has the whole "
        "fleet without ever typing the credential that guards it.",
    ),
    Setting(
        "OWNER_PROXY_SECRET",
        SECURITY,
        "that the owner site's Cloudflare Worker may name the visitor it "
        "forwarded, and that the name is the one it signed",
        "sign-in rate limiting on the owner host counts every visitor as one "
        "client, because the forwarded address chain ends at Cloudflare rather "
        "than at the person: five wrong passkeys from anybody close the sign-in "
        "page for everybody on that host for fifteen minutes. Set the same value "
        "for the Pages build (scripts/build_owner.py) and here.",
        secret=True,
    ),
    Setting(
        "ADMIN_GIVEXP_LIMIT", TUNING,
        "the most XP one /givexp may grant at a time",
        "the compiled limit is used.",
    ),
    Setting(
        "ADMIN_ADDMONEY_LIMIT", TUNING,
        "the most money one /addmoney may grant at a time",
        "the compiled limit is used.",
    ),
    Setting(
        "ADMIN_SETMONEY_LIMIT", TUNING,
        "the most money one /setmoney may set at a time",
        "the compiled limit is used.",
    ),
    Setting(
        "ADMIN_ADDBANK_LIMIT", TUNING,
        "the most money one /addbank may grant at a time",
        "the compiled limit is used.",
    ),

    # --- the database and its backups -------------------------------------- #
    Setting(
        "SUPABASE_URL",
        CAPABILITY,
        "off-host database persistence \u2014 where bot.db is backed up to",
        "the bot runs entirely on the container's own disk. On a free-tier host "
        "that disk does not survive a redeploy, so rank, money and activity are "
        "lost every time the service restarts.",
    ),
    Setting(
        "SUPABASE_SERVICE_KEY",
        CAPABILITY,
        "the credential for that backup bucket, which is private",
        "same as above: no backup, no restore, no snapshots. This key is the "
        "database's password \u2014 it must never reach a log, a page or Discord.",
        secret=True,
    ),
    Setting(
        "BACKUP_KEEP", TUNING,
        "how many hourly snapshots are kept for point-in-time restore",
        "the compiled default of five is used.",
    ),
    Setting(
        "CLOUD_SYNC_ALLOW_SHRINK", TUNING,
        "that a deliberate wipe of the database may be uploaded",
        "a database whose row count has halved is held back rather than "
        "overwriting the backup, which is the protection you want unless you are "
        "deliberately starting over.",
    ),

    # --- how the bot tells you it is unwell -------------------------------- #
    Setting(
        "ERROR_WEBHOOK_URL",
        CAPABILITY,
        "a Discord webhook that unhandled command errors are posted to",
        "failures are written to stdout only \u2014 which on a hosted deploy means "
        "they are visible to whoever opens the host's log, and to nobody else.",
        secret=True,
    ),
    Setting(
        "ALERT_WEBHOOK_URL",
        CAPABILITY,
        "a Discord webhook for the out-of-band alerts \u2014 the one channel that "
        "still works when the gateway does not: the bot was just added, it is "
        "down, or the web surface is being flooded",
        "nothing out of band is sent. Everything that uses this webhook is sent "
        "only when something *noticeable* happens (a new server, a trap page "
        "fetched, a declared crawler that keeps coming back), so an unset value "
        "is quiet rather than broken \u2014 and every event it does send is listed "
        "in PRIVACY.md section 5 and TERMS.md section 12.",
        secret=True,
    ),
    Setting(
        "BOT_SERVER_GUILD_ID",
        CAPABILITY,
        "which server is the bot's home, for bug reports filed where the server "
        "has no bug channel of its own",
        "the compiled default server is used. A report filed in a server that "
        "does have its own bug channel is unaffected.",
    ),
    Setting(
        "UPDATE_CHANNEL_ID",
        CAPABILITY,
        "the channel release notes are posted to",
        "the compiled default channel is used, which exists in the bot's home "
        "server only \u2014 in any other server the update announcement has nowhere "
        "to go.",
    ),
    Setting(
        "UPDATE_ROLE_ID",
        CAPABILITY,
        "the role the update announcement pings",
        "the compiled default role is pinged, which exists in the bot's home "
        "server only.",
    ),
    Setting(
        "RENDER_DEPLOY_HOOK_URL",
        CAPABILITY,
        "the hook /commit uses to actually redeploy this service",
        "/commit still records and announces what it did, but nothing is "
        "deployed: the command reports that no deploy hook is configured.",
        secret=True,
    ),

    # --- the market --------------------------------------------------------- #
    Setting(
        "MARKET_ROLE_IDS",
        CAPABILITY,
        "which roles may use /market",
        "the compiled default role is used, and the gate is absolute \u2014 an "
        "administrator is not an exception. In a server that has no such role, "
        "nobody can reach /market at all.",
    ),
    Setting(
        "MARKET_ROLE_ID",
        TUNING,
        "the singular, older spelling of the same gate",
        "the intended spelling, MARKET_ROLE_IDS, is used. This exists so an "
        "existing deployment that set the old name keeps working \u2014 one of the two "
        "is always unset, and its absence is never on its own a finding.",
    ),

    # --- plans, and whether anything is for sale --------------------------- #
    Setting(
        "PLAN_CHECKOUT", TUNING,
        "whether the paid tiers can be bought",
        "ships off, on purpose: no host shows a Buy button however many checkout "
        "URLs are configured. The tiers are still described on /pricing.",
    ),
    Setting(
        "PLAN_ENFORCEMENT", TUNING,
        "whether a licence is required for the paid systems",
        "ships off, on purpose: switching it on withholds Support Tickets, "
        "Economy, the Market, Starboard and the Activity Tracker from every "
        "server that has not activated a licence.",
    ),
    Setting(
        "LEMONSQUEEZY_CHECKOUT_PRO",
        TUNING,
        "the hosted checkout URL for the Pro tier",
        "intended while nothing is for sale \u2014 /pricing describes the tier and "
        "offers no button. It becomes a problem the moment PLAN_CHECKOUT is 1, "
        "because then the tier is offered with nothing to link to.",
    ),
    Setting(
        "LEMONSQUEEZY_CHECKOUT_ENTERPRISE",
        TUNING,
        "the hosted checkout URL for the Enterprise tier",
        "intended while nothing is for sale, and a gap the moment PLAN_CHECKOUT "
        "is 1 \u2014 the Enterprise tier would be offered with nothing to link to.",
    ),
    Setting(
        "LEMONSQUEEZY_VARIANT_PRO", TUNING,
        "a stable product id, for a store whose products have been renamed",
        "a Pro licence is matched by the product's name instead, which a rename "
        "changes.",
    ),
    Setting(
        "LEMONSQUEEZY_VARIANT_ENTERPRISE", TUNING,
        "a stable product id, for a store whose products have been renamed",
        "an Enterprise licence is matched by the product's name instead, which a "
        "rename changes.",
    ),

    # --- supplied by the platform ------------------------------------------ #
    # Listed because the parity test accounts for every name the code reads, and
    # because "unset" here is the normal state, not a finding. Nobody is ever
    # asked to set one of these.
    Setting("PORT", HOST, "the port the dashboard binds", "the compiled default of 8080 is used."),
    Setting("RENDER", HOST, "that this is running on Render", "detection falls back to the other markers."),
    Setting("RENDER_EXTERNAL_URL", HOST, "the public URL Render assigned", "DASHBOARD_BASE_URL is used instead."),
    Setting("RAILWAY_ENVIRONMENT", HOST, "that this is running on Railway", "detection falls back to the other markers."),

    # --- the tooling that builds the public site --------------------------- #
    # A different machine, and never the running bot. Audited because a build
    # that silently produces a site with no invite button is the same failure
    # this module exists to prevent.
    Setting(
        "LIVE_APP_URL", TUNING,
        "the dashboard the public site's \u201copen the dashboard\u201d links point at",
        "the compiled default is used.",
        scope=BUILD,
    ),
    Setting(
        "SITE_URL", TUNING,
        "the site's own canonical URL, for its metadata and sitemap",
        "the pages build without canonical URLs.",
        scope=BUILD,
    ),
    Setting(
        "OWNER_PUBLIC_SITE_URL", TUNING,
        "where the owner host sends a path it does not serve — the public site, "
        "which is the host whose build has the assets those pages ask for",
        "the compiled default (https://meow-bot.pages.dev) is used. Set it when "
        "the public site moves: the owner host answers only the console's own "
        "paths, and every other one is redirected here.",
        scope=BUILD,
    ),
    Setting(
        "CLOUDFLARE_PROXY", TUNING,
        "whether the build emits a Cloudflare proxy worker for dynamic routes",
        "the static pages are built without it.",
        scope=BUILD,
    ),
    Setting(
        "MEOW_STATIC_SITE", TUNING,
        "that this process is a static host serving files, with no bot behind it",
        "set only by the site build. Unset is correct everywhere else.",
        scope=BUILD,
    ),
    Setting(
        "HUB_URL", TUNING,
        "the hub's own canonical URL, for its sitemap and robots file",
        "the compiled default is used.",
        scope=BUILD,
    ),
    Setting(
        "MEOW_MARKETING_URL", TUNING,
        "where the marketing pages are — the front door and the legal documents. "
        "Read by the hub when it builds a link to them, and by the bot when it "
        "renders `/legal`, so one address serves both rather than each guessing",
        "the site guard's own answer is used, which is the address the watcher is "
        "already checking. Set it only when the site moves and the readers should "
        "follow before the guard does.",
    ),
    Setting(
        "MEOW_LIVE_APP_URL", TUNING,
        "where the published copy reaches the live app, for the figures it cannot "
        "read out of a file: the landing page asks this host for `/api/figures` "
        "when the pages are served from a CDN with no proxy in front of them",
        "unset means the page is served by the process that has the figures, so "
        "it asks itself and needs no address. Set it only for a build that is "
        "not reverse-proxied back to this host.",
        scope=BUILD,
    ),
    Setting(
        "PREVIEW_PING", TUNING,
        "the fake gateway latency the dashboard preview harness reports",
        "the harness's compiled default is used.",
        scope=BUILD,
    ),
    Setting(
        "PREVIEW_GATEWAY", TUNING,
        "which of the three gateway states the dashboard preview harness draws",
        "the preview draws a connected gateway, which is what the harness is "
        "for; the badge reads Live. Only the middle state has no way to be seen "
        "without it, and that state is the one worth looking at.",
        scope=BUILD,
    ),
    Setting(
        "AVATAR_PATH", TUNING,
        "which image file the avatar upload script sends",
        "the script uses the image in the repository's assets folder.",
        scope=BUILD,
    ),
    Setting(
        "BANNER_PATH", TUNING,
        "which image file the banner upload script sends",
        "the script uses the image in the repository's assets folder.",
        scope=BUILD,
    ),
    Setting(
        "SITE_GUARD_INTERVAL_SECONDS", TUNING,
        "how often to check whether the public site is serving repository paths",
        "the default 6-hour interval is used.",
    ),
    Setting(
        "SITE_GUARD_FIRST_CHECK_SECONDS", TUNING,
        "how long after boot to perform the initial site guard check",
        "the default 90-second delay is used.",
    ),
    # The read-only UptimeRobot key. Read-only on purpose: it can list monitors
    # and nothing else, so the worst a leak of it costs is somebody reading which
    # addresses are watched — which is the same thing the public status page it
    # powers already says. Absent is a supported state rather than a fault: the
    # dashboard's Status panel says which variable to set and the bot carries on.
    Setting(
        "UPTIMEROBOT_API_KEY", TUNING,
        "the read-only UptimeRobot key whose monitors the dashboard's status panel shows",
        "the status panel says the integration is off, and nothing is read from "
        "UptimeRobot. \u201cNot configured\u201d and \u201ceverything is down\u201d are "
        "different things, and this is the one that is said.",
    ),
    Setting(
        "UPTIMEROBOT_STATUS_PAGE", TUNING,
        "the public UptimeRobot status page linked from the dashboard",
        "the panel shows no link out.",
    ),
    Setting(
        "UPTIME_INTERVAL_SECONDS", TUNING,
        "how often the bot re-reads UptimeRobot",
        "the default 5-minute interval is used.",
    ),
    Setting(
        "MEDIA_CHECK_INTERVAL_HOURS", TUNING,
        "how often every picture and sound the bot sends is probed for being "
        "dead, which is what stops a rotted link being drawn as an empty box "
        "forever",
        "the default is a week. A dead link then sits in the operator's channel "
        "for up to that long before anything says so — the failure it guards "
        "against has no symptom in the product at all.",
    ),
    Setting(
        "UPTIME_FIRST_CHECK_SECONDS", TUNING,
        "how long after boot to read UptimeRobot for the first time",
        "the default 60-second delay is used.",
    ),
    # --- music -----------------------------------------------------------------
    # Four names, and every one of them is optional: playback does not depend on
    # any of them. They are the *answer* to the one failure a media feature
    # cannot design away (a platform deciding a datacenter address is a bot),
    # which is why they are declared rather than left as an undocumented escape
    # hatch — an operator who needs them has to be able to find them.
    Setting(
        "YTDL_COOKIES", TUNING,
        "a path to a cookies file for the media providers, which is one of the two "
        "ways to answer a provider that has started refusing this host",
        "the extractor runs anonymously. A datacenter address is refused by "
        "YouTube often enough that this is the first thing to reach for when "
        "/play stops resolving and radio still works.",
    ),
    Setting(
        "YTDL_COOKIES_B64", TUNING,
        "the same cookies file, base64-encoded, for a host whose disk does not "
        "survive a deploy",
        "nothing is written and the extractor runs anonymously, exactly as if "
        "YTDL_COOKIES were unset.",
    ),
    Setting(
        "YTDL_PO_TOKEN", TUNING,
        "a proof-of-origin token (``<client>+<token>``) for the providers that ask "
        "for one, which is the other way to answer a provider refusing this host",
        "the extractor asks for none, which is what a fresh deployment does.",
    ),
    Setting(
        "YTDL_VISITOR_DATA", TUNING,
        "the visitor data a proof-of-origin token must be paired with",
        "no visitor data is sent. A token without it is usually rejected, which is "
        "why the two are declared next to each other.",
    ),
    Setting(
        "MEOW_FFMPEG", TUNING,
        "an explicit ffmpeg binary for the music player, for a host whose ffmpeg "
        "is somewhere ``PATH`` does not reach",
        "a system ffmpeg is looked for on ``PATH``, and then the static build "
        "shipped by ``imageio-ffmpeg`` is used — so this is only ever needed to "
        "*override* a working default.",
    ),
    Setting(
        "YTDL_PROBE_TARGET", TUNING,
        "the recording the weekly self-check asks the extractor for, which is how a "
        "stale extractor reports itself instead of every /play failing quietly one "
        "at a time",
        "Blender's own *Big Buck Bunny* upload is used: a recording meant to stay "
        "up, owned by nobody's catalogue and carrying no age gate. Set this only "
        "when the default cannot be reached from where this runs — a probe "
        "recording that gets taken down would report the extractor as broken, "
        "which is the check becoming the fault it exists to find.",
    ),
)

#: Fast lookup for renderers and for anything holding a name from elsewhere.
BY_NAME: dict[str, Setting] = {setting.name: setting for setting in SETTINGS}


# --------------------------------------------------------------------------- #
# Reading the environment
# --------------------------------------------------------------------------- #
def _get(environ, name: str) -> str:
    return (environ.get(name) or "").strip()


def get(name: str, default: str = "") -> str:
    """A setting's value, stripped, or ``default`` when it is absent or blank.

    The runtime's only way to the environment. The stripping is not cosmetic: it
    is what every caller in this tree already did by hand —
    ``(os.environ.get(X) or "").strip()`` — and it is why a blank line in a
    template switches a feature off instead of half-enabling it.
    """
    return _get(os.environ, name) or default


#: What counts as "on". Spelled once, because four modules had four spellings of
#: this set and a deployment could therefore be configured differently by two
#: features reading the same variable.
_TRUTHS = frozenset({"1", "true", "yes", "on"})


def flag(name: str) -> bool:
    """Whether a setting is switched on: ``1``, ``true``, ``yes`` or ``on``.

    Case-insensitive, and absent is off — a missing switch is never a yes.
    """
    return get(name).lower() in _TRUTHS


def is_implied(setting: Setting, environ=None) -> bool:
    """Whether the code already works this setting out from something else set.

    Only the *absence* is excused, never the presence: a host that sets
    ``DASHBOARD_HTTPS=0`` on Render still gets Render's own answer, because the
    code ORs the two. This is about which absences are worth reporting.
    """
    env = os.environ if environ is None else environ
    return any(is_set(name, env) for name in setting.implied_by)


def is_set(name: str, environ=None) -> bool:
    """Whether a setting is present here.

    "Present" means a non-empty value, which is what every caller in this tree
    already tests for — ``(os.environ.get(X) or "").strip()`` — and is why a blank
    line in a template switches a feature off rather than half-enabling it.
    """
    return bool(_get(os.environ if environ is None else environ, name))


def audit(environ=None) -> dict:
    """The whole picture: what is set, what is not, and what that costs.

    ``environ`` is injectable so a test can ask about a deployment that is not
    this one. Values are never read here — only presence. That is a rule about
    the *report*: ``get`` above hands a caller its own setting's value, and
    nothing in this module ever puts one into a page, a log line or JSON.
    """
    env = os.environ if environ is None else environ

    present: list[Setting] = []
    absent: list[Setting] = []
    for setting in SETTINGS:
        if setting.scope != RUNTIME:
            continue
        (present if is_set(setting.name, env) else absent).append(setting)

    # Only what an operator can act on, worst first, then alphabetical so the
    # order is stable between runs.
    attention = sorted(
        (
            setting
            for setting in absent
            if setting.notable and not is_implied(setting, env)
        ),
        key=lambda setting: (SEVERITY.index(setting.kind), setting.name),
    )
    return {
        "status": "attention" if attention else "ok",
        "counts": {
            "total": len(present) + len(absent),
            "set": len(present),
            "unset": len(absent),
        },
        "attention": attention,
        "quiet": sorted(
            (setting for setting in absent if not setting.notable),
            key=lambda setting: setting.name,
        ),
    }


def _by_kind(settings) -> dict[str, list[str]]:
    counts: dict[str, list[str]] = {}
    for setting in settings:
        counts.setdefault(setting.kind, []).append(setting.name)
    return counts


# --------------------------------------------------------------------------- #
# The renderings
# --------------------------------------------------------------------------- #
def _describe(setting: Setting) -> dict:
    """One setting, described — and, deliberately, not read.

    Presence only, for a secret and for a plain value alike: the report exists to
    say *what is missing*, and a page that showed a bot token to answer that
    would have found a worse problem than the one it was looking for.
    """
    return {
        "name": setting.name,
        "kind": setting.kind,
        "what": setting.what,
        "cost": setting.cost,
        "secret": setting.secret,
    }


def attention_json(environ=None) -> list[dict]:
    """The middle rendering: the names behind the word `attention`.

    ``health_json`` counts and does not name — it is public, and "this deployment
    has no sign-in secret" is not a sentence to hand a stranger. ``full_json``
    names everything, which is right for a page about the whole set and wrong for
    a card that says one word.

    This is for the surface in between: one behind the operator's sign-in, where
    the reader is the person who can act. It exists because *"attention" alone is
    a dead end* — a card that reports a number and nothing else sends its reader
    to look somewhere else, and the setting that is actually costing them a
    feature goes unnamed in the place they were already looking.
    """
    return [_describe(setting) for setting in audit(environ)["attention"]]


def health_json(environ=None) -> dict:
    """The public half of the report: how much is unset, and how much matters.

    Public, so it counts and does not name. A stranger reading ``/health`` learns
    that this deployment is missing something — which is true of almost every
    deployment and tells them nothing they can use — and not that it has no
    Discord client secret. The names are one authenticated call away, and the
    path to that call is printed here so it can be found from the outside.
    """
    picture = audit(environ)
    return {
        "status": picture["status"],
        "unset": picture["counts"]["unset"],
        "attention": len(picture["attention"]),
        "detail": "/api/config",
    }


def full_json(environ=None) -> dict:
    """The operator's view: every setting, its purpose and the cost of its absence.

    Presence only; no value is included for any setting, secret or not, so this
    can be served to a signed-in administrator without redaction games.
    """
    env = os.environ if environ is None else environ
    picture = audit(env)
    return {
        "status": picture["status"],
        "counts": picture["counts"],
        "summary": summary_line(env),
        "attention": [_describe(setting) for setting in picture["attention"]],
        "settings": [
            {
                **_describe(setting),
                "scope": setting.scope,
                "set": is_set(setting.name, env),
            }
            for setting in SETTINGS
        ],
    }


def summary_line(environ=None) -> str:
    """One sentence, for a log line or the top of the operator's page."""
    picture = audit(environ)
    counts = picture["counts"]
    kinds = _by_kind(picture["attention"])
    if not picture["attention"]:
        return (
            f"{counts['total']} settings for this process \u2014 {counts['set']} set, "
            f"{counts['unset']} unset, and nothing an operator needs to act on."
        )
    parts = ", ".join(f"{len(names)} {kind}" for kind, names in kinds.items())
    return (
        f"{counts['total']} settings for this process \u2014 {counts['set']} set, "
        f"{counts['unset']} unset. {len(picture['attention'])} of them change what "
        f"this deployment does ({parts})."
    )


def boot_lines(environ=None) -> list[str]:
    """The report as log lines, for the start of a hosted deploy's log.

    One line per actionable absence, each carrying the *cost* rather than the
    purpose: an operator reading a host log at 2am is asking what is broken, not
    what the variable is for. The quiet absences collapse into a count, because a
    list of settings that are correctly unset is noise in the place where signal
    is scarcest.

    The platform's own settings are left out of that count even though they are
    unset here. "RENDER is unset and costs nothing" is true everywhere except on
    Render, and it invites the question "should I set it?" about a variable nobody
    is meant to touch. They are still all accounted for in ``full_json``, which is
    where a complete table belongs.
    """
    picture = audit(environ)
    lines = [f"[CONFIG] {summary_line(environ)}"]
    for setting in picture["attention"]:
        lines.append(f"[CONFIG] \u26a0\ufe0f {setting.kind.upper()} \u2014 {setting.name}: {setting.cost}")
    quiet = [s for s in picture["quiet"] if s.kind != HOST]
    if quiet:
        lines.append(
            f"[CONFIG] {len(quiet)} more are unset and cost nothing "
            f"({', '.join(setting.name for setting in quiet)})."
        )
    lines.append(
        "[CONFIG] Every setting, with what it turns on: GET /api/config "
        "(administrator sign-in). /health reports the count."
    )
    return lines
