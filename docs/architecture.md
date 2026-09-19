# Architecture — how Meow Bot fits together 🗺️

A tour for new contributors. Nothing here is new information — it just connects
the files so you know where to look.

## The big picture

```mermaid
flowchart LR
    U[Discord member<br/>types /pay] --> D[Discord API]
    D --> B[bot.py<br/>global checks + sync]
    B --> C[cogs/economy.py<br/>command callback]
    C --> F[utils/features.py<br/>is this feature on?]
    C --> M[utils/modules.py<br/>per-server toggle state]
    C --> H[utils/* helpers<br/>money / XP / prices]
    H --> DB[(utils/db.py<br/>SQLite bot.db)]
    C --> S[utils/style.py<br/>answer card]
    S --> D
    A[Admin<br/>/help ⚡ Open Dashboard] --> K[keep_alive.py<br/>Flask dashboard]
    K --> M
    K --> DB
```

Two processes in one: `bot.py` (Discord gateway) and `keep_alive.py` (Flask web
dashboard in a background thread). They share state through `utils/modules.py`
— the **only** reader/writer of on/off toggles — so the dashboard and the bot
can never disagree.

## What happens when someone runs a slash command

1. **Discord** routes `/pay` to the bot process.
2. **`bot.py → global_module_check`** runs first for *every* command:
   - DMs? Only `DM_ALLOWED_COMMANDS` may run outside a server.
   - Always-allowed (`help`, `dashboard`, `sync`, …)? Through.
   - Otherwise look up `MODULE_COMMAND_MAP` (built from `utils/features.py`)
     and refuse with a "turned off, re-enable in the dashboard" card if any
     required feature is off for that server.
3. **Permission checks** run (declared once in `utils/permissions.py`,
   enforced by discord.py decorators or an in-callback guard).
4. **The cog callback** (`cogs/*.py`) does the work, calling `utils/*`
   helpers. Money moves inside `db.run_atomic()` transactions so two
   simultaneous commands can't spend the same coin twice.
5. **The reply** goes out as a card via `utils/style.py` (`send_card`),
   which suppresses accidental pings by default.

## Where things live (and why)

| Question | Answer |
|---|---|
| Which features exist? | `utils/features.py` — one `Feature(...)` each; cogs, dashboard cards, help menu, defaults all derive from it |
| Is feature X on for server Y? | `utils/modules.py` — cached, thread-safe, shared with the dashboard |
| Who may run command Z? | `utils/permissions.py` — `COMMAND_ACCESS` table + picker-visibility stamping |
| Where is data stored? | `utils/db.py` — SQLite (`bot.db`), migrated on boot |
| Where do the tests and the preview write? | *Not* `bot.db`, and this is load-bearing rather than tidy. `tests/__init__.py` points the whole suite at a throwaway file in a temporary directory — `__init__.py` and not `conftest.py`, because CI discovers with `unittest`, which never loads a conftest — and `scripts/preview_dashboard.py` copies `bot.db` aside before it imports `keep_alive`. Both used to write the operator's live database: 3,680 test fixtures (guild `777001`) landed in `scheduled_messages`, and a click on the preview's bulk toggle switched every module off in the Development server, which the bot then uploaded as the production backup. `tests/test_db_isolation.py` fails if either drifts, and it checks the live file's size and mtime, not just the code path |
| How do commands reach Discord? | `bot.py → sync_commands()` on every boot, started as a **background task** (`_publish_commands_in_background`) and never awaited: Discord paces command writes, so a drifted server takes minutes to publish and the boot owes its backup loop, its channel binding and its release note *now* — compare-then-upsert, **per server**, never bulk-overwrite (a rejected bulk sync once wiped a guild to 1 command). A guild's list is that guild's enabled commands — Discord cannot hide a global command in one server, so the list itself is per server, and the shared/global scope keeps only what works with no server to resolve (`help`, `legal`) |
| Which commands does one server have? | `utils/command_scope.py` — pure, one rule read by the publisher, `force_sync.py` and the dashboard panel. Hidden = declared by the feature registry **and** turned off by that server's own modules; a name nobody declares is never hidden. Republished when a module switch flips (`bot.resync_commands`) and on join |
| Can the bot look different per server? | yes — `utils/identity.py`: Discord keeps `nick`, `avatar`, `banner` and `bio` on the bot's **member** in each guild, so they are per server by construction. `table guild_identity` holds what was asked for; Discord holds what is live. Who may: an administrator of that server via a guild-scoped dashboard link, or the operator's passkey. The application profile stays `/bot avatar` / `/bot banner` (owner only) |
| How does data survive redeploys? | `utils/cloud_sync.py` — restore from Supabase on boot, upload every 25s, final flush on SIGTERM |
| What is the site itself? | one stylesheet and one page, at the project root: `static/meow.css` is the whole design system (tokens, surfaces, buttons, the aurora behind every page, one focus ring, and a `prefers-reduced-motion` block) and every template links it — a page's own CSS, where it has any, comes *after* it so the page keeps winning. `/` is `templates/home.html`, the front door for somebody who is not signed in: real figures from `BOT_INSTANCE`, the feature registry's own list of systems, the invite, and an honest status line that says *Starting up* rather than counting nobody's servers. `/privacy` and `/terms` are `templates/legal.html` rendered from `utils/webdocs.py` — a deliberately small markdown subset (escaped first, `http(s)` links only) over the *same* files the Discord cards read, because Discord's 6000-character budget is a reason to tighten wording, not a reason a policy should be unlinkable |
| Where is the public site published? | **Cloudflare Pages**, built by `scripts/build_site.py` and deployed by `.github/workflows/site.yml` — the bot stays on Render, because a static host cannot answer a request. The build runs in GitHub Actions and only the finished directory is handed over, because Cloudflare's build image has no Python; two repository secrets (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`) turn the publish on, and without them the job skips with a notice instead of going red. It was written for **GitHub Pages** first and Pages is not available here at all: the repository is private and the owner is on GitHub's free plan, so the API answers `422 Your current plan does not support GitHub Pages for this repository`. Three things the build has to get right, each a failure it already had: every root-relative URL a template emits (`/static/meow.css`, `/pricing`) is rewritten to a relative one, which is what makes the output portable to a host serving from a `/<repo>/` subpath *and* one serving from a root — and a stylesheet's `url()` is rewritten **differently** from a page's link, because `meow.css` already sits inside `static/`, which is precisely the difference that silently cost the cat cursor (`static/static/cursor-*.svg`, a 404 no browser reports, so the only symptom is the system arrow); the build renders through the **real** Flask app's test client rather than a second renderer, so the published copy and the page the bot serves cannot drift into disagreeing about the product; and every page that needs a running process (`/dashboard`, `/login`, `/servers`, the market, `/health`) is rewritten to the live app instead of to a path a static host cannot serve. `_site/` is generated and git-ignored, never committed |
| Where's the dashboard? | `keep_alive.py` + `templates/` — three ways in (Discord sign-in, one-time magic link, passkey), module toggles, message composer, scheduler, per-server command scope, per-server bot identity, live market pages |
| Who may open the dashboard for a server? | whichever way in the session used. A **Discord sign-in** (`utils/discord_auth.py`) asks for `identify`+`guilds`, keeps the servers where the person holds Manage Server/Administrator/owners *and* the bot is in, and the session may reach only those (`_resolve_guild_id`, scope `user`); a **one-time link** from `/help` is scoped to the server it was minted in; the **passkey** is the operator's and reaches every server. `/servers` is the picker for a Discord session, and every server-scoped route re-checks the list on each request, so a server the bot has left or the person was demoted from stops being reachable immediately |
| What is a server entitled to, and who paid for it? | `utils/plans.py` — the plan model (what each tier includes, how many servers and how many scheduled messages) plus the Lemon Squeezy licence that proves it. A licence is activated **per server** with `/bot plan`, and Lemon Squeezy counts activations against the limit on the licence, so "up to 5 servers" is the store's rule rather than a number this bot invents. Validation is the store's **public** licence API, so no API key is stored anywhere and no webhook endpoint has to exist; a key for a product this deployment does not sell is handed straight back, and a network failure never downgrades a server. **Selling ships off** (`PLAN_CHECKOUT`) and **enforcement ships off** (`PLAN_ENFORCEMENT`), both read fresh every time, and they are separate switches on purpose: filling in a checkout link while a store is being set up is configuration, not a decision to charge. The selling switch is checked in exactly one place — `plans.checkout_url`, which every surface that offers a plan for money already goes through — so no page has to remember to ask. While selling is off there is no Buy button on any host and the tiers read as where the project is heading; while enforcement is off every server keeps every system, and the pricing page says so in words. When it is on, the withholding is applied in exactly one place — `utils/modules.get_states`, which the command gate, the per-server command list and the dashboard all read — so a switch, a picker and a refusal cannot disagree about the same server. `tests/test_plans.py` pins both states, and the prices on `/pricing` come from this same model so the site cannot advertise a tier the code lacks |
| How do you add the bot to another server? | `discord_auth.invite_url()` — the Discord authorize URL with `bot`+`applications.commands`, offered in the dashboard header and on `/servers` (with `guild_id`+`disable_guild_select` when a specific server is meant) |
| Where do dashboard messages go? | `utils/scheduled.py` — one row per message, whether it is still queued or was posted the moment it was written (`source`) — plus `keep_alive.py` (the delivery tick, started from `bot.py`) |
| How does an update get shipped? | `/bot push` (`cogs/bot_config.py`) — with a Render Deploy Hook in `RENDER_DEPLOY_HOOK_URL`, `utils/deploy.py` POSTs it and the boot announces the release itself; with no hook the command says no code moved and posts this version's notes through `bot.announce_release_notes_to_guild`. The bot cannot commit: it can only deploy what is already on GitHub. Its card goes to the server's `test` channel, falling back to `update` |
| Where does the bot post in *this* server? | `utils/bot_channels.py` — `update` / `error` / `bug` / `commands` / `log` / `test` are per-guild settings, bound by channel *name* the first time a server is seen (`#update`, `#bot-error`, `#bug-report`, `#bot-cmd`, `#logs`, `#test-ground`) and authoritative after that — matched exactly first, then with decoration stripped, so `💬・bot-cmd` binds the same role. The release note's ping resolves the same way: the role id the bot has always pinged where that server has it, otherwise a role named `Dev` / `Admin` / …, otherwise nothing. A hardcoded channel id here is a bug: it names exactly one server, which is why the update channel, the failure channel, level-up cards and bug reports were all invisible everywhere else. A **bug report** is the one message allowed to leave the server it was filed in: it is posted in that server's own bug channel, and in the bot's own server (`BOT_SERVER_GUILD_ID`, whose `#bug` is found by name even before anything has been bound) when this server has none — so a report can never be refused for want of a channel, and it is never a ticket, which is the ticket panel's job for members' own problems. Two roles, `guilds` and `cmdlog`, belong to **the bot's own server only** (`roles_for` refuses them everywhere else, so no member server can bind them even by naming a channel to match): `#active-servers` and `#real-time-cmd-data` are the operator's log of who has the bot and what people run with it — see the row below |
| What does the operator learn about the bot itself? | `utils/operator_log.py`, fed by four events in `bot.py`: `on_guild_join` (a server added the bot — logged with its size, owner and, when View Audit Log allows, who added it; the same handler also binds its channels and sends it the release note, each step isolated so one failure cannot hide the others), `on_guild_remove` (removed, deleted, or banned — all one event), `on_app_command_completion` (every command, with the runner, the server, the channel and the moment), and `tree.on_error` (`command_failed` — the command, the runner, where it ran and the reason, which is the entry the operator needs most and the one completion can never produce, because discord.py dispatches completion only in the `else` branch of its own call: a command that raised never reaches it, so a broken bot used to leave an empty channel that read as "nobody uses this"). Three rules, each a failure this project already had: an entry may never break the thing it logs (every failure is reported on the host and swallowed), an entry may never ping (a card carries `NO_PINGS`, which matters because a server's *name* is a stranger's text and `@everyone` is a legal server name), and what a person **typed** is never copied into the log — the command, its runner and its outcome are the record, not its arguments. `bot_server_guild_id()` falling back on a malformed `BOT_SERVER_GUILD_ID` is what keeps a typo in an environment variable from costing the entry. A failure's *reason* is carried only when it comes from Discord (a transport fact); a Python-level error is recorded by its type alone, because a message raised while handling what somebody typed can quote it back and the log promises never to copy that. `report_second_instance` is the fourth rule's opposite number: a boot that finds another live copy of the bot — the same database lease that stops two copies fighting over the backup, `cloud_sync.other_live_copy`, read for the gateway hazard — posts one card naming it, because two sessions of one application receive every interaction, one answers, and the loser's command ends in *Interaction has already been acknowledged* while the ones that reach a quiet session are answered too late to count. `announce_setup` posts the one-time card that explains both channels, on the boot that binds them and never again (it is posted from what that boot *wrote*, not from a read-back of the store, so a write that has not landed cannot cost the card that exists to explain the channel), and `_notice_missing` reports the first entry that finds nothing bound — into the update, then error, then log channel, once per role per process, because an installed feature that writes nowhere is otherwise indistinguishable from one that works |
| Why is a command missing from the picker — or still in it after being removed? | `utils/commands_sync.py` compares, `bot.py` publishes, `force_sync.py` does the same job from a terminal. Three rules, each already paid for: **read → compare → write**, because Discord allows 200 command writes a day per scope and a blind re-upload spends them on nothing; **upsert one command at a time, never a bulk overwrite**, because a rejected bulk sync has already deleted the scope's whole list (this bot was once left with a single command); and **a name leaves the picker only by being on `RETIRED_COMMANDS`**, because the publisher can only add or replace — a folded command has no local counterpart, so it is neither missing nor stale and nothing else would ever remove it. That list is load-bearing: Discord allows **100 chat-input commands per scope**, and twelve folded names (`/dashboard`, `/sync`, `/updatedb`, `/setavatar`, `/setbanner`, `/addlevelrole`, `/removelevelrole`, `/privacy`, `/tos`, `/meowfee`, `/activity-top`, `/activity-channel`) held the last of that budget, so creating `/bot` and `/legal` was answered with ``30032 Maximum number of application commands reached``. A refusal of one command must not stop the ones behind it — the scope is only full for a *new* name, and updates are still allowed — but a spent daily budget must stop everything, because that window is closed for all of them. Whichever it is, the boot prints the reason: a scope that published nothing used to say only *"published 0, 32 still pending"*, which is how a picker missing two commands and offering twelve dead ones stayed that way for weeks. The `/bot push` command and every deploy go through this same path. |

## The dashboard's five jobs

1. **Module toggles** (`/api/toggle`) — persist via `utils/modules.py`, then
   schedule an immediate cloud backup so a restart can't undo the click.
2. **Message composer** (`/api/send-message`) — posts as the bot, max 5
   channels, every mention re-resolved against the live guild (a browser can
   send anything, so nothing it says is trusted).
3. **Scheduled messages** (`/api/schedule-message`) — the same builder as
   "send now", stored instead of posted, and delivered from a tick on the bot's
   own loop. Claiming a due message is one conditional `UPDATE`, so a message is
   posted exactly once no matter how many things look at it at the same moment.
   An immediate send is written to the same table as a finished row
   (`utils.scheduled.record_sent`), which is the only record it leaves: the
   receipt lives in the page, and "which channels got that announcement?" has to
   outlive it. Only `pending` rows are ever delivered, so a record can never be
   posted twice. A delivery that did **not** arrive — refused, partly refused,
   skipped for being too late, or interrupted by a restart — is posted as a card
   in the guild's `log_channel_id`, tagging whoever queued it
   (`keep_alive._alert_schedule_outcome`); with no usable log channel that person
   is DM'd instead. A message that went out is not news: it is visible in the
   channel it landed in.
4. **Live market pages** (`/market/<SYMBOL>`) — public JSON + canvas chart
   fed by the same price snapshot the commands trade on.
5. **The public pages** (`/`, `/privacy`, `/terms`) — no session, no gateway
   dependency for the two policies, and every figure on the landing page either
   measured or absent.

## Conventions that keep 600+ tests green

- **Declare, don't duplicate.** Feature? → `features.py`. Audience? →
  `permissions.py`. The tests diff the registry against the real command tree
  and fail on drift.
- **Transactions for money.** Any function that moves coins uses
  `db.run_atomic()` with conditional writes (`UPDATE … WHERE balance >= …`).
- **Pure helpers where possible.** `utils/rewards.py`, `utils/levels.py`,
  `utils/meowfee.py` take plain arguments (no `discord` import) so tests can
  call them directly.
- **Cards never ping by accident.** `send_card` defaults to
  `AllowedMentions.none()`; only `/announce` opts in, deliberately.
