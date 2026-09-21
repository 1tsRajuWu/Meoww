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
| What does Discord actually hold — and did the republish land? | `utils/command_publish.py` — the other half of the scope panel, and a different claim from *which commands does one server have* (that one is what the server **asked for**). Every publish pass writes its outcome down at one choke point (`bot._sync_scope`, so boot, both drains, the bulk publish and every module switch are covered by construction, plus the two paths that raise and never return a status at all) and the dashboard reads it: whether Discord's copy was **read**, how many commands are **still to write**, when the pass finished, and Discord's own words when something went wrong — four tones the panel draws with the Status panel's own dots. Three rules, each a defect it was written for: **a count that is ambiguous is not printed** (`held` is `None` when a pass was refused part-way through its writes, because a figure under a heading is read as a measurement and an approximate one is worse than none); **the sentence has one author** (`describe()` composes it and the template prints `text`, never assembling it from the numbers, because this box is painted twice — by the server and by the page's own refresh — and two renderers that each spell their own sentence is how the Status panel came to name one environment variable twice; the ok case is **one of three mutually exclusive shapes**, agrees / disagrees / no count to compare, because composed as an opening with the disagreement appended it read "Discord holds 87 commands, matching what this server's switches say. Discord holds 87 — 3 fewer than …"); and **wait for the event, not for a duration** (`/api/toggle` answers with the timestamp of the publish the click supersedes and the panel asks again until that advances — two readings of the server's own clock, never the browser's against the server's, so a host in another region cannot make the wait forever or give up early). A scope nobody has published reads as *not read yet* rather than as agreement, and a page with no server selected asks about **its own** empty scope rather than Discord's shared lane, which holds the DM-safe few |
| Can the bot look different per server? | yes — `utils/identity.py`: Discord keeps `nick`, `avatar`, `banner` and `bio` on the bot's **member** in each guild, so they are per server by construction. `table guild_identity` holds what was asked for; Discord holds what is live. Who may: an administrator of that server via a guild-scoped dashboard link, or the operator's passkey. The application profile stays `/bot avatar` / `/bot banner` (owner only) |
| How does data survive redeploys? | `utils/cloud_sync.py` — restore from Supabase on boot, upload every 25s, final flush on SIGTERM |
| What is the site itself? | one stylesheet and one page, at the project root: `static/meow.css` is the whole design system (tokens, surfaces, buttons, the aurora behind every page, one focus ring, and a `prefers-reduced-motion` block) and every template links it — a page's own CSS, where it has any, comes *after* it so the page keeps winning. `/` is `templates/home.html`, the front door for somebody who is not signed in: real figures from `BOT_INSTANCE`, the feature registry's own list of systems, the invite, and an honest status line that says *Starting up* rather than counting nobody's servers. `/privacy` and `/terms` are `templates/legal.html` rendered from `utils/webdocs.py` — a deliberately small markdown subset (escaped first, `http(s)` links only) over the *same* files the Discord cards read, because Discord's 6000-character budget is a reason to tighten wording, not a reason a policy should be unlinkable |
| Where is the public site published? | **Cloudflare Pages**, built by `scripts/build_site.py` and deployed by `.github/workflows/site.yml` — the bot stays on Render, because a static host cannot answer a request. The build runs in GitHub Actions and only the finished directory is handed over, because Cloudflare's build image has no Python; two repository secrets (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`) turn the publish on, and without them the job skips with a notice instead of going red. It was written for **GitHub Pages** first and Pages is not available here at all: the repository is private and the owner is on GitHub's free plan, so the API answers `422 Your current plan does not support GitHub Pages for this repository`. Three things the build has to get right, each a failure it already had: every root-relative URL a template emits (`/static/meow.css`, `/pricing`) is rewritten to a relative one, which is what makes the output portable to a host serving from a `/<repo>/` subpath *and* one serving from a root — and a stylesheet's `url()` is rewritten **differently** from a page's link, because `meow.css` already sits inside `static/`, which is precisely the difference that silently cost the cat cursor (`static/static/cursor-*.svg`, a 404 no browser reports, so the only symptom is the system arrow); the build renders through the **real** Flask app's test client rather than a second renderer, so the published copy and the page the bot serves cannot drift into disagreeing about the product; and every page that needs a running process (`/dashboard`, `/login`, `/servers`, the market, `/health`) is rewritten to the live app instead of to a path a static host cannot serve. `_site/` is generated and git-ignored, never committed |
| Where's the dashboard? | `keep_alive.py` + `templates/` — three ways in (Discord sign-in, one-time magic link, passkey), and **seven pages rather than one scroll**: module toggles, message composer, scheduler, per-server command scope, per-server bot identity, status. The rail is those pages, `aria-current` is what the server sets rather than a scroll spy, and `Ctrl`/`⌘`+`K` opens a palette that indexes all of them. The owner's console (`/owner` and three siblings: servers, infrastructure, output) is the other axis — one server at a time *versus* which server is the wrong one |
| Does the bot do anything nobody asked for? | exactly one thing, and it is opt-out: `utils/company.py` + `cogs/company.py` — when a voice channel's only occupant is one person, the bot joins it (deafened **and** muted), says hello, and leaves the moment its reason to be there is gone. It arrived slowly (a 12-second grace, because somebody alone for a moment is somebody whose friend is reconnecting) and leaves quickly, it follows somebody *between* rooms but not out of voice altogether (leaving is an answer, remembered for five minutes), and it holds one connection because a Discord client can only be in one channel at a time. `/company mode:Off` is the switch, server-scoped and administrator-only, and the module is declared in `features.py` and `permissions.py` like any other. **A bot cannot watch a Go Live stream** — Discord has no API for it and the gateway carries no video — so the card says the person is live and never pretends to show a picture, and the owner's Live page shows the same fact per person (`self_stream`) beside who is alone right now |
| What decides how the web surfaces look? | two sheets and a test. `static/meow.css` is every public page (the dashboard, the hub, the landing page, the two policies and every sign-in form) and `console/src/styles.css` is the operator's console, and each is written to the same five laws: **60-30-10** (one background, one raised surface, one accent spent on what can be acted on), **a scale rather than a pile of one-offs** (body text 16–18px, every size from a `--type-*` token), **one accent** (three state colours may only report a state), **nothing reachable only with a mouse** (one `:focus-visible` ring per sheet, 44×44 for anything a finger hits, an `outline: none` only where the same rule draws a replacement) and **chunked navigation** (nine console pages grouped into three, never one list of nine). `tests/test_design_laws.py` is what makes those sentences true rather than aspirational: it computes WCAG contrast from the palette tokens in every theme, measures the body size, reads the headings, the `alt` attributes, the outline rules and the target floors out of the stylesheets, walks the registry for the nav arithmetic, and fails if a console rule writes a colour literal of its own |
| How many servers and people does the bot actually reach? | `utils/figures.py` — the one module that turns this deployment's own counters into a page a **stranger** reads, which is why it is four rules and not a query. **Counts, never names** (a landing page may say *1,669 people reached*; it may not say which twelve — the per-channel and per-member breakdowns stay behind the dashboard's sign-in); **a reading that was never taken is absent, not zero** (a sleeping free-tier host has no gateway to ask, and a confident *0 servers* reads as a bot nobody uses); **nothing is fetched while a request is being answered** (two indexed local reads, cached for a minute, so a refresh loop costs one query rather than one per visitor); and **the windows belong to the tracker** (`today`/`week`/`month`/`all` are `utils/digest`'s own, with `tests/test_figures.py` comparing the arithmetic so the two cannot drift). Served live by `/api/figures` — which carries a cross-origin allowance for the site's own address, because the published copy is a file on a CDN asking a browser-side question and a refused `fetch` reaches no banner — and rendered server-side into both the landing page's figures cards and the console's *What the bot is doing* panel |
| Who may open the dashboard for a server? | whichever way in the session used. A **Discord sign-in** (`utils/discord_auth.py`) asks for `identify`+`guilds`, keeps the servers where the person holds Manage Server/Administrator/owners *and* the bot is in, and the session may reach only those (`_resolve_guild_id`, scope `user`); a **one-time link** from `/help` is scoped to the server it was minted in; the **passkey** is the operator's and reaches every server. `/servers` is the picker for a Discord session, and every server-scoped route re-checks the list on each request, so a server the bot has left or the person was demoted from stops being reachable immediately |
| What is a server entitled to, and who paid for it? | `utils/plans.py` — the plan model (what each tier includes, how many servers and how many scheduled messages) plus the Lemon Squeezy licence that proves it. A licence is activated **per server** with `/bot plan`, and Lemon Squeezy counts activations against the limit on the licence, so "up to 5 servers" is the store's rule rather than a number this bot invents. Validation is the store's **public** licence API, so no API key is stored anywhere and no webhook endpoint has to exist; a key for a product this deployment does not sell is handed straight back, and a network failure never downgrades a server. **Selling ships off** (`PLAN_CHECKOUT`) and **enforcement ships off** (`PLAN_ENFORCEMENT`), both read fresh every time, and they are separate switches on purpose: filling in a checkout link while a store is being set up is configuration, not a decision to charge. The selling switch is checked in exactly one place — `plans.checkout_url`, which every surface that offers a plan for money already goes through — so no page has to remember to ask. While selling is off there is **no premium surface at all** — no tiers, no prices, no Buy buttons — and `/pricing` is a free product's page: one plan, every system from `utils/features.py`, the price from this model, and a note that paid plans may come later. The page used to keep describing the tiers with only the buttons switched off, which advertises a shop that is not open; the whole surface now appears and disappears together with the switch (`keep_alive.pricing_page` passes `selling` and the template branches on it once). While enforcement is off every server keeps every system, and the pricing page says so in words. When it is on, the withholding is applied in exactly one place — `utils/modules.get_states`, which the command gate, the per-server command list and the dashboard all read — so a switch, a picker and a refusal cannot disagree about the same server. `tests/test_plans.py` pins both states, and the prices on `/pricing` come from this same model so the site cannot advertise a tier the code lacks |
| How do you add the bot to another server? | `discord_auth.invite_url()` — the Discord authorize URL with `bot`+`applications.commands`, offered in the dashboard header and on `/servers` (with `guild_id`+`disable_guild_select` when a specific server is meant). **Two grants, always offered together**: the default asks for `Administrator` (one bit, so no feature can ever arrive as `Missing Permissions`), and `invite_url(minimal=True)` builds the least-privilege link from `MINIMAL_PERMISSION_BITS`, each entry naming the call site that needs it. `keep_alive._invite_context()` builds both — or neither, when no client id is set — so a page cannot offer one without the other |
| Where do dashboard messages go? | `utils/scheduled.py` — one row per message, whether it is still queued or was posted the moment it was written (`source`) — plus `keep_alive.py` (the delivery tick, started from `bot.py`) |
| How does an update get shipped? | `/bot push` (`cogs/bot_config.py`) — with a Render Deploy Hook in `RENDER_DEPLOY_HOOK_URL`, `utils/deploy.py` POSTs it and the boot announces the release itself; with no hook the command says no code moved and posts this version's notes through `bot.announce_release_notes_to_guild`. The bot cannot commit: it can only deploy what is already on GitHub. Its card goes to the server's `test` channel, falling back to `update` |
| Where does the bot post in *this* server? | `utils/bot_channels.py` — `update` / `error` / `bug` / `commands` / `log` / `test` are per-guild settings, bound by channel *name* the first time a server is seen (`#update`, `#bot-error`, `#bug-report`, `#bot-cmd`, `#logs`, `#test-ground`) and authoritative after that — matched exactly first, then with decoration stripped, so `💬・bot-cmd` binds the same role. The release note's ping resolves the same way: the role id the bot has always pinged where that server has it, otherwise a role named `Dev` / `Admin` / …, otherwise nothing. A hardcoded channel id here is a bug: it names exactly one server, which is why the update channel, the failure channel, level-up cards and bug reports were all invisible everywhere else. A **bug report** is the one message allowed to leave the server it was filed in: it is posted in that server's own bug channel, and in the bot's own server (`BOT_SERVER_GUILD_ID`, whose `#bug` is found by name even before anything has been bound) when this server has none — so a report can never be refused for want of a channel, and it is never a ticket, which is the ticket panel's job for members' own problems. Two roles, `guilds` and `cmdlog`, belong to **the bot's own server only** (`roles_for` refuses them everywhere else, so no member server can bind them even by naming a channel to match): `#active-servers` and `#real-time-cmd-data` are the operator's log of who has the bot and what people run with it — see the row below |
| What does the operator learn about the bot itself? | `utils/operator_log.py`, fed by four events in `bot.py`: `on_guild_join` (a server added the bot — logged with its size, owner and, when View Audit Log allows, who added it; the same handler also binds its channels and sends it the release note, each step isolated so one failure cannot hide the others), `on_guild_remove` (removed, deleted, or banned — all one event), `on_app_command_completion` (every command, with the runner, the server, the channel and the moment), and `tree.on_error` (`command_failed` — the command, the runner, where it ran and the reason, which is the entry the operator needs most and the one completion can never produce, because discord.py dispatches completion only in the `else` branch of its own call: a command that raised never reaches it, so a broken bot used to leave an empty channel that read as "nobody uses this"). Three rules, each a failure this project already had: an entry may never break the thing it logs (every failure is reported on the host and swallowed), an entry may never ping (a card carries `NO_PINGS`, which matters because a server's *name* is a stranger's text and `@everyone` is a legal server name), and what a person **typed** is never copied into the log — the command, its runner and its outcome are the record, not its arguments. `bot_server_guild_id()` falling back on a malformed `BOT_SERVER_GUILD_ID` is what keeps a typo in an environment variable from costing the entry. A failure's *reason* is carried only when it comes from Discord (a transport fact); a Python-level error is recorded by its type alone, because a message raised while handling what somebody typed can quote it back and the log promises never to copy that. `report_second_instance` is the fourth rule's opposite number: a boot that finds another live copy of the bot — the same database lease that stops two copies fighting over the backup, `cloud_sync.other_live_copy`, read for the gateway hazard — posts one card naming it, because two sessions of one application receive every interaction, one answers, and the loser's command ends in *Interaction has already been acknowledged* while the ones that reach a quiet session are answered too late to count. `announce_setup` posts the one-time card that explains both channels, on the boot that binds them and never again (it is posted from what that boot *wrote*, not from a read-back of the store, so a write that has not landed cannot cost the card that exists to explain the channel), and `_notice_missing` reports the first entry that finds nothing bound — into the update, then error, then log channel, once per role per process, because an installed feature that writes nowhere is otherwise indistinguishable from one that works |
| Why is a command missing from the picker — or still in it after being removed? | `utils/commands_sync.py` compares, `bot.py` publishes, `force_sync.py` does the same job from a terminal. Three rules, each already paid for: **read → compare → write**, because Discord allows 200 command writes a day per scope and a blind re-upload spends them on nothing; **upsert one command at a time, never a bulk overwrite**, because a rejected bulk sync has already deleted the scope's whole list (this bot was once left with a single command); and **a name leaves the picker only by being on `RETIRED_COMMANDS`**, because the publisher can only add or replace — a folded command has no local counterpart, so it is neither missing nor stale and nothing else would ever remove it. That list is load-bearing: Discord allows **100 chat-input commands per scope**, and twelve folded names (`/dashboard`, `/sync`, `/updatedb`, `/setavatar`, `/setbanner`, `/addlevelrole`, `/removelevelrole`, `/privacy`, `/tos`, `/meowfee`, `/activity-top`, `/activity-channel`) held the last of that budget, so creating `/bot` and `/legal` was answered with ``30032 Maximum number of application commands reached``. A refusal of one command must not stop the ones behind it — the scope is only full for a *new* name, and updates are still allowed — but a spent daily budget must stop everything, because that window is closed for all of them. Whichever it is, the boot prints the reason: a scope that published nothing used to say only *"published 0, 32 still pending"*, which is how a picker missing two commands and offering twelve dead ones stayed that way for weeks. The `/bot push` command and every deploy go through this same path. |

## The dashboard's pages

Neither console is one document any more. `DASHBOARD_PAGES` (seven) and
`OWNER_PAGES` (four) are the single list each rail renders from, every route
resolves to one page id, and each page renders only its own section — so the
composer's listeners only register where a composer exists and the scheduler's
poll only starts where a queue does. Two consequences worth knowing before
editing either template:

* **The script is one file per console, guarded at each element.** A page is a
  set of sections behind `{% if page == … %}`; the script below them is shared,
  so a listener registered on a null element would take the rest of the script
  with it. That is why the composer, the scheduler and the bulk switches each sit
  behind a `const …Here = Boolean(...)`.
* **A control that lives on another page is reached by hash.** The palette's
  actions name the page they need, and a cross-page one navigates to
  `/dashboard/systems#btn-enable-all`; the destination page's shell focuses the
  element it names on load. A link to a page is not a link to a control.

The pages, and what each owns:

| Page | Owns |
| --- | --- |
| `/dashboard` | ping, systems on, members, commands offered — the one-screen answer |
| `/dashboard/systems` | all eleven switches, the filter, the bulk toggles |
| `/dashboard/composer` | the message builder, channel picker, tag search, receipt |
| `/dashboard/schedule` | the queue, the history, cancel and send-now |
| `/dashboard/identity` | per-server nick, avatar, banner, bio |
| `/dashboard/commands` | the per-server command scope and whether the republish landed |
| `/dashboard/status` | monitors, command publish, this deployment's own readings |

And the jobs themselves:

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

## The message layer (`utils/style.py`)

Every non-interactive message is a Components V2 card, and V2 is stricter than an
embed in three ways that have each been a shipped bug:

- **An attachment is invisible unless a component references it.** Discord's
  component reference says it in one line: *"Attachments won't show by default -
  they must be exposed through components"*. The upload succeeds, the API accepts
  the message, and nothing is drawn — which is why `/meowsound` had "no sound"
  while looking fine. `send_card(file=…)`/`files=…` now appends a File component
  for each upload, and skips the card entirely (legacy send) if the installed
  library has no File component, where an attachment is at least visible as a file.
- **Mentions notify, so they must be opted into.** A `TextDisplay` mention pings
  its target; `send_card` therefore defaults to `AllowedMentions.none()`, and a
  card that is *about* somebody passes `style.pings(target)`. Without that, a card
  can name two people and reach neither — which is what `/action`, `/rizz` and
  `/roast` did. `pings()` names `everyone` and `roles` as `False` because
  discord.py treats an omitted field as *allowed*.
- **A picture has to exist.** Cards resolve GIFs through `utils/gifs`, which
  fetches from keyless providers and only uses a URL after a probe confirms an
  `image/*` body; nothing confirmed means no picture, never a broken one.

A bot's presence has one more rule of the same kind: the word in front of the
status ("Playing …") is Discord's, chosen by the activity *type*. A line written
as a headline renders as "Playing Meowing at the wall", so shipped lines read as
continuations, and `custom` exists for a line with no verb at all.

### The card's type and spacing scale

Discord draws four sizes and one hairline, and a card assembled without a rule
puts a title, five bold labels and a footer at the same volume. So `utils/style`
owns the scale and the builders are the only place a marker is written:

- **`title()`** is the subject — the only level-two heading on a card, so a body
  that opens with its own `##` is demoted to a group rather than arguing with the
  title. **`section()`** is a group inside a card; body text is unmarked; and
  **`label()`** (Discord's small `-#`) is what a value *is*. A field is therefore a
  quiet label over body text: read down to find the field, across to get it.
- **Spacing is four named levels** — `GAP`, `BREAK`, `RULE`, `RULE_LARGE` — placed
  by `_place()`, which holds three invariants: never a spacer as a card's first or
  last child, never two spacers side by side (the stronger one wins), never a rule
  where it separates nothing. `clean_text()` trims every string, collapses runs of
  blank lines, and drops a hand-drawn divider that would otherwise sit directly
  above the card's own rule.
- **One accent, four states.** `ACCENT` plus `SUCCESS`/`WARN`/`DANGER`/`INFO`, and
  `accent_for()` is the only door: a value `ACCENT_ALIASES` knows becomes the state
  it means, anything else becomes the accent. That is what the 251 `0x` literals in
  the cogs meet on the way in.
- **New content has one place to go**: `Panel.append()`, which inserts above the
  closing rule, so a merged embed's later fields or a route's extra line cannot
  land *under* the footer's hairline.

`scripts/preview_cards.py` renders the cards without sending one: it builds the
fixtures through the real module, reads them back out of `to_components()` — the
JSON Discord receives — and draws two columns, the working tree and any revision
of `utils/style.py` loaded out of git, with the component/spacer/accent counts
beside each. It exists because a card can otherwise only be seen in Discord, and a
preview drawn from a hand-written mock would agree with whatever its author
believed.

### The media watch (`utils/media_health.py`)

"A picture has to exist" is checked at use time, which leaves the other half open:
a hand-typed link in the tree can rot for as long as nobody looks at that card, and
a dead link is the quietest failure in the product — Discord accepts the message,
draws an empty box and reports nothing anywhere.

`keep_alive.start_media_health` runs one pass on the bot's own loop, on a clock kept
in the database (`MEDIA_CHECK_INTERVAL_HOURS`, a week by default) rather than in the
process, because Render restarts this bot often enough that an in-memory timer would
mean a sweep of every URL on every deploy — and a check that costs something on
every boot is a check that gets switched off. A pass probes two kinds of target:

- **hardcoded** media, *found* by scanning the files that may hold one rather than
  listed by hand, skipping tests, docs and retired cogs (a deliberately broken URL
  in a test is not a fault to report);
- **resolved** media — one target per action and per pet, read off `utils/gifs`'s own
tables, so an action added there is covered without anybody remembering this module.

Three rules, all of which were learned from the failure this was built for: a `200`
is not evidence (a body whose content type is not media is reported as *not media*),
**a clean week is silence** (a weekly "everything is fine" is how a channel gets
muted), and a pass **never raises** — a target that could not be asked lands in
`unchecked`, kept apart from `dead`, because "this link is broken" and "I could not
reach anybody" are different sentences and only the first one sends somebody to edit
a URL. Findings go to the operator channel via `operator_log.media_alert`, naming the
target, the file and line, and what the probe got; counts (never names) are on
`/health` as its own `media` block; `scripts/check_media.py` runs the same pass by
hand and exits non-zero on a dead link.

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
  `AllowedMentions.none()`; only `/announce` and the cards that are *about* a
  person (`/action`, `/rizz`, `/roast`) opt in, deliberately and by name.
