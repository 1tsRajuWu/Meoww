# Deployment Setup Guide

## Current Deployment Configuration

### 1. GitHub
- Repository: discord-bot
- Branch: main
- Status: Active and synced
- Deployment: Manual deployment to Render

### 2. Render Configuration
- Service: Meoww — **the name on Render's dashboard**, which is not the same
  string as its address
  - Address: https://meow-discord-bot.onrender.com (its `onrender.com`
    subdomain, chosen when the service was created on 2026-09-12 and kept
    through the rename)
  - Dashboard: https://dashboard.render.com/web/srv-daiqibp594qs739qebc0
- Runtime: Python 3.11
- Plan: Free tier
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

**The name and the address are two different strings, and writing one where the
other belongs creates a second service that cannot start.** `render.yaml` is the
Blueprint, and Render matches a Blueprint against the account **by name**: it
updates the service with the declared name if one exists, and creates a new one if
none does. That file declared `meow-discord-bot` — the subdomain, copied from the
URL — while the service that has run this bot since 2026-09-12 is named `Meoww`,
so a Blueprint sync on 2026-09-19 created `meow-discord-bot-fmhp` beside it. The
duplicate was never given any environment variables, because the prompts below are
answered per service and nobody answered them for that one, so **every deploy it
ever had ended at boot in `RuntimeError: DISCORD_TOKEN not found`** and Render
never ran an instance on it. It was deleted on 2026-09-23; the manifest and this
line carry the name the account actually has, and `tests/test_config_audit.py`
refuses the edit that caused it.

What that law can and cannot see is worth stating. A file cannot ask Render what
the service is called, so it cannot catch the account drifting away from the
repository — that is a dashboard fact. What it can do is hold the two places the
repository writes the name to each other (this section and `render.yaml`), and
refuse a name that *is* the address, which is the shape the drift took.
Consequences worth keeping: a sync is now able to adopt the running service
instead of minting a stranger — the manifest's build command, start command, plan
and runtime are the live service's own, read back from Render's API, which is the
state in which a sync changes nothing, though **no sync has been run** to prove
adoption and an environment variable's *value* cannot be read back this way, so
the prompts below are the part worth looking at before pressing Sync — and the
account is meant to hold exactly two services, this one and `discord-github-bot`
(a different repository, seen next to `Meow` in the dashboard). Anything else
appearing there is a sync that did not match a name in this file.

### 3. Supabase Storage (Database Backup)
- Purpose: Persistent database storage across deployments
- Configuration:
  - SUPABASE_URL: Supabase project URL
  - SUPABASE_SERVICE_KEY: Service role key for storage access
  - Bucket: bot-backups
  - Backup files:
    - bot.db (current state)
    - bot-db-YYYYMMDD-HHMMSS.db (snapshots)
  - Retention: 5 snapshots, rotated hourly
  - Sync interval: Every 25 seconds

### 4. Cloudflare Pages (the public site)
- Project: `meow-bot` → https://meow-bot.pages.dev
- Source: this GitHub repository, connected through Pages' own Git integration,
  deploying on every push to `main`. `Site` in GitHub Actions builds the same
  output but **cannot publish** — its token has `Pages Read` and not
  `Pages Write` — so the Git integration is what actually ships the site, and a
  failing `Site` run is not a failing deploy.
- **A failing workflow is not a failing deploy, and the workflow is failing.**
  Every run of `Site` *and* `tests` on this account stops before a job starts
  (`recent account payments have failed or your spending limit needs to be
  increased`), so nothing in Actions builds or publishes anything, and the red
  mark on every push is about the account rather than about the commit. The Git
  integration above is unaffected — it is Cloudflare's own build, off the
  repository — and the deliberate path is now one command, which is what to use
  when a push has to be *proved* to have landed:

  ```bash
  python scripts/ship.py                 # build, upload and verify both hosts
  python scripts/ship.py --dry-run       # build both hosts, upload nothing
  python scripts/ship.py --verify-only   # upload nothing; did the push arrive?
  python scripts/ship.py --only meow-owner
  ```

  `--verify-only` reports what each host serves, and a host that is *behind* is
  the honest answer rather than a failure of the check: `meow-bot` publishes
  itself on push within seconds, while `meow-owner` (no Git integration, below)
  stays where it was last deployed — so a docs-only commit legitimately leaves it
  a commit behind, and `--only meow-owner` is the fix when the console changed.

  It refuses to build from a dirty tracked file or from a commit that is not on
  `origin/main` — a deployment carries the commit it was built from, and both of
  those produce a hash that does not name its contents — then uploads both
  projects as production deployments of that commit and asks Cloudflare, and the
  hosts themselves, whether it arrived. It is the local twin of `site.yml` for
  the part of that workflow that has never once run here.
- **Build command:** `pip install -r requirements.txt && python scripts/build_site.py --out _site --proxy`
- **Build output directory:** `_site`

Both of those settings are load-bearing, and the output directory is the one to
watch. Cloudflare Pages is handed a directory and told *everything in it is
public*. Leave the build command and output directory empty and Pages publishes
the **repository root** instead — which is a public copy of `bot.py`, `utils/`,
`cogs/`, `render.yaml` and `.env.example` at the site's own address. That is not
a theory: it is what this project was doing, and the Pages build log said so in
plain words (`No build command specified. Skipping build step.`, then
`Uploading... (185/185)` — the repository has exactly 185 tracked files). No
secret value leaked, because every secret lives in the host's environment rather
than in the tree, but the source was readable by anyone until it was caught.

`scripts/build_site.py` now **refuses** to build into the repository root, so
the other half of that mistake cannot be made quietly.

**A cached copy can outlive the deploy that put it there, and it looks exactly
like this failure.** When the mistake above was corrected, seven paths kept
answering `200` with real source — and the deployment was clean. Pages keeps a
previous deployment's assets served for a while so that a rollout cannot break
an app mid-visit, so those responses carried `cf-cache-status: HIT` and a growing
`age` while every deployment host answered `404`. Re-deploying does not clear it
(three deploys followed and the copies stayed) and `Purge Everything` does not
reach a `*.pages.dev` host.

**And they do not expire on their own, which this page claimed for a day and a
measurement on 2026-09-20 disproved.** The same seven paths were still being
served then, a day and four deployments after the fix, with `age: 65296` on a
response carrying this project's own `max-age=300, must-revalidate` — a stored
copy outliving its own stated lifetime, and every client that asked for a *fresh*
reading with `Cache-Control: no-cache` got the stored copy anyway. There is no
remedy inside this repository: the purge is a Cloudflare-side action on a host
the project does not own, so the honest state of a `cached` verdict is **open
until somebody at Cloudflare closes it**, not "wait and it will go".

That also means **the check speaks for the location it asks from**, and no more.
Run from the bot's own host the same module answered `cached: 0` and `clean` for
six paths that a request from another region was still being handed a file from:
a *deployment* is one fact read from anywhere, a *cached copy* lives in whichever
edge location stored it. This matters for reading the check below: **a `200` at
the bare URL is not by itself proof of a leak, and a `404` is not proof of safety
either** — the next section has the version that answers the real question.

The output directory must contain `_worker.js` and `_routes.json` (the builder
writes both). They are what reverse-proxies `/login`, `/dashboard`, `/servers`,
`/api`, `/market` and `/health` through to Render, so the dashboard and the
sign-in page live on the Pages domain rather than 404ing there. A deployment
missing them answers **404** for every one of those paths — which is how this
was diagnosed.

#### Checking it from a terminal

Ask with a unique query string on every path. That is not a formality: it is the
difference between asking the deployment and asking a cache that remembers an
older one, and only the first answer tells you what is published now.

```bash
# Every one of these must be 404: the source is not a web page.
for p in /bot.py /utils/cloud_sync.py /cogs/economy.py /render.yaml /.env.example; do
  printf '%-24s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "https://meow-bot.pages.dev$p?cachebust=$RANDOM"
done

# These must NOT be 404: the proxy is running.
for p in /login /health; do
  printf '%-24s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "https://meow-bot.pages.dev$p"
done
# /dashboard and /servers answer 302 -> /login when signed out, which is correct.
```

#### The check that runs on its own

The bot watches this on a timer, because the mistake above has no symptom in the
product: every page looked correct while the tree was public, and the deploy
reported **success**. `utils/site_guard.py` asks the site for nine paths that
exist in this repository and says something when one of them answers. It runs
**in the bot process** rather than in CI for a reason this project learned the
hard way — CI was switched off for an hour and a half by a billing failure, and
a guard against a publishing mistake must not share a failure mode with
publishing.

It asks with a cache-buster, so its answer is about the deployment; it asks the
bare URL afterwards to tell a stale copy from a real leak; and it asks one path
that does not exist, because a host that answers everything would make every
other answer meaningless. Findings go to the operator's channel — an exposure
every cycle, and a stale copy, a blind check or a catch-all host once a day. The
state is on `/health` as `site_guard`, counts and no names.

```bash
curl -s https://meow-discord-bot.onrender.com/health | python3 -m json.tool | grep -A6 site_guard
# "status": "clean"      the nine paths all answer 404 at the deployment
# "status": "cached"     the deployment refuses them; a stored copy still answers
# "status": "exposed"    the deployment is publishing the checkout — act on it
# "status": "unreachable"  the site did not answer; nothing was established
# "status": "fallback"   the host answers every path, so the check cannot tell
```

Tune it with `SITE_GUARD_INTERVAL_SECONDS` (default six hours) and
`SITE_GUARD_FIRST_CHECK_SECONDS` (default ninety, so a deploy is re-checked
shortly after boot).

### 5. Cloudflare Pages (the owner console)

- Project: `meow-owner` → https://meow-owner.pages.dev
- Source: **no Git integration.** `wrangler pages project list` reports a Git
  provider for `meow-bot` and none for this project, and every deployment it has
  carries a commit stamped by hand — so a push to `main` updates the public site
  and leaves this one untouched. That is the whole mechanism behind "the changes
  are not showing": the console bundle here was built from `ed5dee8` while the
  repository was three commits further on.

Deploy it deliberately, after `npm run build` inside `console/` — which
`scripts/ship.py` does as one command, including the check that the host took it:

```bash
cd console && npm run build   # writes static/console/
cd .. && python scripts/ship.py --only meow-owner
```

The three commands it replaces, for when one of them has to be run on its own:

```bash
cd console && npm run build          # writes static/console/
python scripts/build_owner.py        # writes _owner/ with that bundle inside
npx wrangler pages deploy _owner --project-name=meow-owner --branch=main
```

Two ways to stop doing that by hand, in order of preference:

1. **Connect the project to the repository** in the Cloudflare dashboard with
   build command `pip install -r requirements.txt && python scripts/build_owner.py --out _owner`
   and build output directory `_owner`. A Pages build image has no Node, which
   is exactly why `static/console/` is committed: the build copies the bundle
   rather than compiling it.
2. **Add a publish step to `.github/workflows/site.yml`** for
   `wrangler pages deploy _owner --project-name=meow-owner`. It needs
   `CLOUDFLARE_API_TOKEN` with **Pages: Edit**; the token this repository has
   been using cannot publish at all (`Pages Read` only), which is why the
   `Site` job builds and does not deploy.

The same console is *also* served by the bot itself at
`https://meow-bot.pages.dev/owner` (the site's worker proxies `/owner` to
Render, and Render redeploys on every push), so a hand-deployed `meow-owner` is a
second, cacheable copy of one app — worth knowing when the two disagree, and the
reason the answer is "deploy it" rather than "the console is wrong".

#### This host serves the console and nothing else

`_owner/_worker.js` answers a **short prefix list** — `/owner`, `/api/console`,
`/api/toggle`, `/api/toggle-all`, `/api/command-scope`, `/api/image`,
`/login/discord`, `/logout`, and `/static/` from its own CDN — and **301s every
other path to `OWNER_PUBLIC_SITE_URL`** (default `https://meow-bot.pages.dev`),
with the path and query string preserved. The root is the console sought directly
(`/` → `/owner`), so a bookmark to the bare hostname lands on the fleet.

This is a fix for a *class* of bug rather than an untidiness. As a reverse proxy
of the whole application this host had two properties that both cost real time:
every page the backend renders arrives referring to `/static/...`, which only
exists here if the build copied it — so a missing stylesheet 404'd **on this host
alone**, silently (that is how `/dashboard` came back as a heading over a black
rectangle) — and every door the backend has was a door here too, which is how the
console grew a second entrance whose guard nobody was reading (`/login` answered
200 on this domain). Both are gone: the asset contract is one directory, and the
only sign-in this host proxies is the console's own (`/login` is *deliberately*
not on the list). `tests/test_owner_site.py` derives the paths the bundled
console actually calls out of `console/src/` and fails if one is not covered, and
reads the prefix list back out of the generated worker, so the two cannot drift.

Moving the public site? Set `OWNER_PUBLIC_SITE_URL` for this build as well; the
compiled default is the `meow-bot` host.

#### The strongest setting to add, and it is not in this repository

The path list above is what makes **Cloudflare Access** (Zero Trust) practical
here: an Access application covering `/owner*` and `/api/console/*` asks for an
identity at the edge, before the request reaches Render, and the passkey stays
as the app-side layer rather than the only thing standing in the doorway. Hiding
the whole hostname behind Access is *not* recommended — `/static/` and the
sign-in redirect have to stay reachable for a signed-out browser to be told what
it is missing — so scope the policy to the two prefixes and leave `/` and
`/static/*` public. Verify Access is entitled on the plan before relying on it.

Nothing about this is enforced in the repository, which is exactly why it is
written down here: a change made only in a dashboard is a change the next reader
of this file cannot see. The passkey and the per-address sign-in limit
(`OWNER_PROXY_SECRET`) remain correct on their own — Access is one more layer,
not a substitute for either — and the Render host deliberately keeps serving
`/owner` with only the passkey, so that a mistake in an edge policy cannot lock
the operator out of their own console.

## Free, and online without you

This deployment is a free Render web service plus a free Supabase project, and
"will it still be up if I stop touching it" has three parts — one of which is not
in this repository at all, and that is the one that matters most.

**1. Something outside the host has to knock on it.** A free Render instance is
stopped after fifteen minutes without an inbound request, and nothing inside the
container can prevent that: the timer is on Render's side. An external monitor is
what keeps it awake, and this deployment already reads one — UptimeRobot's free
plan is fifty monitors at five-minute intervals, and the same read-only key
(`UPTIMEROBOT_API_KEY`) is what turns the dashboard's status panel from *not read
this process* into the history this host actually has. Add one HTTP(s) monitor
pointing at `{DASHBOARD_BASE_URL}/health`: that endpoint is public on purpose and
answers `200` with counts and no names, so it is safe to hand to a third party.
Expect 30–60 seconds of cold-start latency on the first request after a nap.

Nothing is broken without a monitor — the bot is simply offline between requests
and comes back on the next one — but **an update window the process slept through
is still owed**, because nothing is recorded as announced until Discord accepted
the message. A nap delays that morning's release note; it does not lose it.

**2. The hours are a budget, and one always-on service spends nearly all of it.**
Render's free plan allows 750 instance hours per account per month, and a service
that never sleeps uses 730–744 of them (24 × 30, or 24 × 31). This bot may be the
**only** free service on the account: a second one running the same way pushes the
total over the allowance and both are suspended — the failure looks like "the bot
died" and has nothing to do with the bot. That is the price of the duplicate in
§2, and the reason the dashboard's service list is worth a glance after any
Blueprint sync: a name the account does not have does not fail the sync, it
quietly spends the budget building a second bot. The public site and the owner console
are on Cloudflare Pages, which has no such budget, so there is nothing to add
there and nothing to keep awake.

**3. Assume the disk is temporary, because it is.** Render's filesystem is wiped
on every deploy, and the database is a SQLite file on it. What makes that
survivable is `utils/cloud_sync.py`: the database is restored from Supabase
Storage at boot, snapshotted on a timer (`SNAPSHOT_INTERVAL`, one hour), and —
the part that matters for a host that sleeps — **flushed on `SIGTERM`**, which is
the signal Render sends before it stops an idle instance. A shutdown is therefore
a save rather than a loss, and the shrunken-database guard
(`SHRINK_GRACE_TICKS`, `CLOUD_SYNC_ALLOW_SHRINK`) is what stops a wiped local file
from being mistaken for the truth and uploaded over the good copy.

What this does *not* cover: anything written in the last moments before a hard
kill with no warning, and any deployment where `SUPABASE_URL` /
`SUPABASE_SERVICE_KEY` were never set — without them there is no backup at all,
which `/dbstatus` and the dashboard's Cloud card both say out loud.

## Environment Variables Required

### Discord Bot
- `DISCORD_TOKEN`: Bot token from Discord Developer Portal

### Supabase Storage
- `SUPABASE_URL`: Your Supabase project URL
- `SUPABASE_SERVICE_KEY`: Service role key (not anon key)

### Optional
- `ENABLE_MESSAGE_CONTENT_INTENT=1`: Enable message content intent
- `ENABLE_MEMBERS_INTENT=1`: Enable members intent
- `ERROR_WEBHOOK_URL`: Error reporting webhook
- `UPTIMEROBOT_API_KEY`: the **read-only** UptimeRobot key (My Settings -> API
  Settings -> Read-Only API Key). Turns the dashboard's status panel from "not
  read yet in this process" into the history this host has actually had, and
  lets `/health` say whether the bot has been up *lately* rather than only
  whether it is up now. It is declared in `render.yaml`, so the deploy prompts
  for it — undeclared, Render never asks and the panel stays off with no error
  to show for it, which is the omission this manifest has been caught by before
- `UPTIMEROBOT_STATUS_PAGE`: a public UptimeRobot status page to link from that
  panel for visitors who are not signed in
- `UPDATE_WINDOW_IST`: the times of day, in IST, when a deploy's release notes
  may go out in each server's own update channel. Defaults to
  `03:33,04:00,04:44` — the small hours, three times a few minutes apart so a
  free host that was asleep at the first still lands in the second or third. A
  boot outside the window sends nothing and records nothing, so the note is still
  owed and goes out complete when the window opens: it is a digest of everything
  since that server's last one, so a delay is a delay and never a gap. Set it to
  `always` to post the moment a deploy boots (the behaviour before this existed),
  and `SILENT_UPDATES=1` for the other end, which posts no automatic note at all.
  `/bot push` posts one on demand whatever either says, because a person asking
  is not what is being silenced
- `SILENT_UPDATES=1`: stop the automatic release notes entirely — the one on a
  boot that changed `CHANGELOG.md`, and the one a server gets the moment it adds
  the bot. Only automated notes: `/bot push` still works. This is the *mute*
  switch, not the *quiet* one; the quiet one is `UPDATE_WINDOW_IST`
- `DASHBOARD_HTTPS=1`: force the dashboard's session cookie to `Secure`. Render
  and Railway are detected automatically; set this when you terminate TLS
  yourself (the server binds `0.0.0.0`, so without it a plaintext request to the
  dashboard port would carry the session cookie)

## Deployment Process

### One-off repairs (posting as the bot, by hand)

`scripts/repair_untagged_actions.py` is the shape to copy for anything that has to
*act* as the bot once, in one place, without a redeploy. It always prints what it
would do first and needs `--apply` to write anything, and it records what it has
already done in `guild_settings` so running it twice is a no-op.

```bash
# dry run: reads history, posts nothing
.venv/bin/python scripts/repair_untagged_actions.py --guild Hmm --channel general \
    --date 2026-09-21 --hours 1-5 --tz +05:30

# and then, once that output reads correctly
.venv/bin/python scripts/repair_untagged_actions.py --guild Hmm --channel general \
    --date 2026-09-21 --hours 1-5 --tz +05:30 --apply
```

It needs `DISCORD_TOKEN` (environment or `.env`) and is read-only until `--apply`.
Channel names are matched with their decoration stripped, because real channels are
`#💬・general` and an exact-name lookup finds nothing.

### Initial Setup
1. Create Supabase project
2. Create storage bucket named "bot-backups"
3. Get SUPABASE_URL and SUPABASE_SERVICE_KEY
4. Add environment variables to Render
5. Deploy from GitHub

### Automatic Backup System
The bot includes automatic cloud sync:
- **Restore**: On startup, restores latest backup from Supabase
- **Backup**: Every 25 seconds, uploads database changes
- **Snapshots**: Hourly timestamped snapshots
- **Retention**: Keeps 5 most recent snapshots
- **Protected Tables**: economy, user_levels, mod_cases, warnings, activity_messages, activity_voice

### Deployment Safety Features
- Single-writer lease prevents conflicts
- Database shrinkage detection
- Automatic retry on network failures
- SIGTERM handler for clean shutdown
- WAL mode for concurrent access

## Recent Changes (Activity Tracking Fixes)

### Bug Fixes Applied
1. **Admin-only permissions**: Activity commands now require Administrator permission
2. **New channel tracking**: Added listener for on_guild_channel_create to track new channels
3. **Data persistence**: Added activity_messages and activity_voice to COUNTED_TABLES
4. **Error handling**: Added permission error handlers for better user feedback

### Files Modified
- `cogs/tracker.py`: Added admin checks, new channel listener, error handlers
- `utils/permissions.py`: Changed activity commands to ADMIN level
- `utils/cloud_sync.py`: Added activity tables to backup protection

## Verification Steps

### Pre-deployment
1. Test activity tracking locally
2. Verify admin permissions work
3. Test new channel creation tracking
4. Check database backup system

### Post-deployment
1. Verify bot connects to Discord
2. Test activity commands with admin account
3. Check Supabase bucket for backups
4. Monitor logs for backup sync status

## Troubleshooting

### Backup Issues
- Check SUPABASE_URL and SUPABASE_SERVICE_KEY are set
- Verify bucket "bot-backups" exists in Supabase
- Check logs for "[CLOUD SYNC]" messages

### Permission Issues
- Verify user has Administrator permission
- Check Discord Developer Portal for bot permissions
- Review utils/permissions.py for command access levels

### Tracking Issues
- Verify tracker module is enabled in guild settings
- Check logs for "[TRACKER]" messages
- Ensure bot has necessary intents

## Monitoring

### Key Log Messages
- `[TRACKER]`: Activity tracking events
- `[CLOUD SYNC]`: Backup/restore operations
- `[PERMISSIONS]`: Command permission changes
- `[SYNC]`: Command tree synchronization
- `[SITE-GUARD]`: whether the public site is serving this repository (once per
  cycle, and the line to grep when asking *was the tree public, and when*)
- `[UPTIME]`: one line per refresh with UptimeRobot's own status and detail —
  the line that says `Not connected` when `UPTIMEROBOT_API_KEY` is unset, so an
  integration that is off is never mistaken for one that is broken
- `[UPDATE]`: where the release notes are. At boot, either `Posted update
  announcement to #...`, `Holding this deploy's release notes for 03:33 IST
  tomorrow (...)`, `automatic release notes are off (SILENT_UPDATES=1)`, or the
  one line that says which servers have no update channel to post them in. The
  windowed line is what tells you a deploy is *waiting* rather than broken, and
  it names the exact moment the note will go out

### Health Checks
- Bot online status
- Command permissions
- Backup sync frequency
- Database integrity
- `GET /health` -> `uptime`: monitor counts and the weakest 30-day ratio. Counts
  rather than names, because the endpoint is unauthenticated; the rows live in
  `/api/uptime` behind the dashboard's sign-in.