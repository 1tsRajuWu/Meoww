<div align="center">

# 🐾 Meow Bot

[![Tests](https://github.com/1tsRajOwO/meow-discord-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/1tsRajOwO/meow-discord-bot/actions/workflows/tests.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-blurple.svg)](https://discordpy.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An all-in-one Discord bot: moderation, tickets, leveling, economy, a live
market, starboard, social commands — plus a real-time web dashboard.**

New here? Start at [⚡ 5-minute quickstart](#-5-minute-quickstart). Everything
else is linked from the [table of contents](#contents).

</div>

## Contents

- [⚡ 5-minute quickstart](#-5-minute-quickstart)
- [✨ Features](#-features)
- [🗂️ Project structure](#️-project-structure)
- [🔧 Full setup guide](#-full-setup-guide)
- [🚀 Hosting (24/7)](#-hosting-247)
- [🎛️ Dashboard](#️-dashboard)
- [💳 Plans & licences](#-plans--licences)
- [📚 Docs & deep-dives](#-docs--deep-dives)
- [🧪 Tests](#-tests)
- [🆘 Troubleshooting](#-troubleshooting)
- [🔐 Security](#-security)
- [🤝 Contributing](#-contributing)

---

## ⚡ 5-minute quickstart

1. **Create the bot** at https://discord.com/developers/applications → New
   Application → **Bot** → **Reset Token** → copy it (that's your
   `DISCORD_TOKEN`, shown only once).
2. **Turn on intents** on the same Bot page: **Server Members Intent** +
   **Message Content Intent**.
3. **Invite it**: the simplest way is an invite button on the dashboard or the
   front page. Two are offered, and they are a real choice — **Administrator**
   (one permission, so nothing can ever report `Missing Permissions`) or the
   explicit list. By hand: OAuth2 → URL Generator → scopes `bot` +
   `applications.commands` → either permission. See
   "[What the invite asks for](#what-the-invite-asks-for)" below.
4. **Run it**:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # paste DISCORD_TOKEN into .env
   python bot.py
   ```
5. In Discord, type `/` — you'll see every command. Try `/help`, then
   `/rank`. Done! 🎉

> Stuck? See [full setup](#-full-setup-guide) and
> [troubleshooting](#-troubleshooting). Never share your `.env`.

---

## ✨ Features

| | Feature | What you get |
|---|---|---|
| 🛡️ | **Moderation & Logs** | `/ban /kick /mute /warn /purge`, mod cases, `#logs` audit trail |
| 🎟️ | **Support Tickets** | Button panel → private staff channels (`/ticket setup`) |
| 🏆 | **Leveling & XP** | Message + voice XP, level-up cards, `/rank /leaderboard` |
| 💰 | **Economy & Casino** | `/daily /work /rob /pay`, shop with 20 real perks, slots, blackjack… |
| 📈 | **Market** | Trade 72 real crypto/stocks with coins at live prices |
| 🤖 | **Automod & Shield** | Anti-spam flood protection, invite/link filters |
| ⭐ | **Starboard** | Star reactions → community showcase cards |
| 🐾 | **Social Roleplay** | `/action /marry /ship /hunt /zoo`, `/uwu` styler |
| 🎭 | **Fun & Memes** | Live Reddit memes, 8ball, quotes, roasts |
| 🎮 | **Mini-Games Hub** | TicTacToe, RPS, trivia, mines, crash, high/low |
| 📊 | **Activity Tracker** | Per-day, per-channel message + voice stats |
| ⚙️ | **System (always on)** | `/serverinfo /userinfo /announce`, lockdown, `/bot sync`, `/bot push` |

Every feature can be toggled per-server from the dashboard — no redeploy.
Retired features (music, giveaways, …) are archived intact in
[`deleted_features/`](deleted_features/README.md) with restore steps.

---

## 🗂️ Project structure

```
meow-discord-bot/
├── bot.py                 ← start here: intents, cog loading, command sync
├── keep_alive.py          ← web dashboard (Flask, runs in a thread)
├── force_sync.py          ← re-register slash commands from a terminal
├── requirements.txt  Procfile  Dockerfile  render.yaml  .dockerignore
├── .env.example           ← copy to .env and fill in (never commit .env)
├── cogs/                  ← one file per feature (economy.py, market.py, …)
├── utils/                 ← shared helpers; features.py = the feature registry
├── templates/             ← the pages: dashboard, sign-in, picker, market, landing, policies
├── static/                ← meow.css (the one design system), logo, favicon, cat cursors
├── assets/                ← fonts + sounds the bot ships with
├── scripts/               ← helper tools (avatar/banner, dev runner, preview, build_site.py)
├── docs/                  ← deployment, voice-debug + architecture guides
├── tests/                 ← 600+ unit tests (stdlib only, no network)
├── deleted_features/      ← retired features, kept but never imported
└── .github/               ← CI + the GitHub Pages publish workflow, issue forms, PR template
```

**Reading order for newbies:**
`bot.py` → [`utils/features.py`](utils/features.py) →
[`docs/architecture.md`](docs/architecture.md) → any file in `cogs/`.
Each folder has its own `README.md` explaining what's inside in plain words.

---

## 🔧 Full setup guide

### Step 1 — Create the bot on Discord's Developer Portal

1. Go to https://discord.com/developers/applications
2. **New Application** → name it → **Create** → sidebar **Bot** →
   **Reset Token** → copy it. **Save it somewhere safe — shown only once.**
3. Same page → **Privileged Gateway Intents** → turn ON **Server Members
   Intent** and **Message Content Intent**, then mirror that in `.env`:
   ```
   ENABLE_MESSAGE_CONTENT_INTENT=1
   ENABLE_MEMBERS_INTENT=1
   ```
   (Off by default on purpose: requesting an intent the portal hasn't enabled
   makes Discord refuse the whole connection. Without message-content intent
   the bot still runs, but message text is invisible — automod can't filter
   links and XP uses the baseline rate.)
4. Sidebar **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`
   → **Administrator**, or the narrower list under "What the invite asks for"
   below → open the generated URL to invite the bot.

### Step 2 — Get the files + configure

```bash
git clone https://github.com/1tsRajOwO/meow-discord-bot.git
cd meow-discord-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Paste `DISCORD_TOKEN=your_token_here` into `.env`.
**Never share this file.** Leaked token? Reset it in the portal immediately.

Local dev loop: `bash scripts/run-dev.sh` (auto-restarts on crash).

### Step 3 — Set up tickets

1. Create a role called exactly **Staff** (recommended — staff auto-get ticket access).
2. In the channel for the panel, run `/ticket-setup` → a "Need Help?" embed
   with a button appears; clicks open private ticket channels.

### Step 4 — Try the rest

Type `/` anywhere or run `/help`. First tries: `/rank`, `/lockdown`,
`/ban @someone spamming`, `/automod-antispam enabled:true`.

---

## 🚀 Hosting (24/7)

Full walkthrough: [`docs/deployment.md`](docs/deployment.md) (Render free
tier + Supabase backups, env-var table, backup safety, monitoring).

Short version: push to GitHub → create a Render web service
(`pip install -r requirements.txt` / `python bot.py`) → set `DISCORD_TOKEN`
(+ `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` for persistent DB,
`FLASK_SECRET_KEY`, `ADMIN_PASSKEY`) → deploy. The bot restores its database
from Supabase on boot and re-saves every 25s.

---

## 🎛️ Dashboard

Open `/help` in your server and tap **⚡ Open Dashboard** → you get a
**single-use sign-in link** (expires in 15 minutes) to the web admin panel:
toggle modules in real time, compose announcements as the bot, and open live
market charts. No separate account — Discord itself is the login.

Three ways in, and each is for a different person:

| Way in | Who it is for | What it reaches |
|---|---|---|
| **Sign in with Discord** | anyone who helps run a server | only the servers you have *Manage Server* in, and only while the bot is in them |
| **One-time link** (⚡ Open Dashboard on `/help`) | an administrator who wants in quickly | the one server the link was made in |
| **Admin passkey** | the operator | every server the bot is in |

The rest of the site needs no session at all: **`/`** is the landing page (what
the bot is, what it is doing now, how to add it), and **`/privacy`** and
**`/terms`** are the same documents the Discord commands show, rendered from the
same files — and **`/market/BTC`** is the live chart, public like the price
data it draws.

---

## 💳 Plans & licences

`/pricing` describes three tiers and `utils/plans.py` is the model behind them,
so the page and the bot cannot disagree about what "Pro" means. Buying is
handled by **Lemon Squeezy**; the bot never sees a card, and it validates the
licence key with the store's own public licence API, so there is no API key to
store and no webhook endpoint to expose.

**Every system is free today, and nothing is on sale.** Two switches hold that
state, and both are committed at `0` so a deploy has one definite answer instead
of a prompt somebody answers differently on each host:

| Switch | Off (shipped) | On |
|---|---|---|
| `PLAN_CHECKOUT` | no Buy button anywhere, even with the links below filled in; the tiers read as where the project is heading | the paid cards link at the store |
| `PLAN_ENFORCEMENT` | every server keeps every system; plans are recorded and shown | a server without a licence is held to the free tier — moderation, automod, leveling, fun, games and social — and to one server |

They are separate on purpose. Filling in a checkout link while a store is being
set up is configuration, not a decision to start taking money, and switching on
the limits takes systems away from servers using them today.

To sell plans:

1. In Lemon Squeezy, turn **license keys on** for both products (product →
   Settings → Generate license keys) and set the activation limit: 5 for Pro,
   unlimited for Enterprise. That limit *is* the "up to 5 servers" rule — the
   store counts the activations, not the bot. Until this is on a buyer receives
   no key to activate.
2. Paste each product's checkout link (product → **Share → Copy link**) into
   `LEMONSQUEEZY_CHECKOUT_PRO` and `LEMONSQUEEZY_CHECKOUT_ENTERPRISE`. Nothing is
   shown for them yet — see the next step.
3. Set `PLAN_CHECKOUT=1` when you actually want to sell. Until then the pricing
   page carries **no premium surface at all** — no tiers, no prices, no Buy
   buttons, however many checkout links are configured — and reads as a free
   product's page with a note about what may come later. That is the state to
   configure the store in: the page describes one product, and the tiers only
   reappear when they are really on sale.
4. In a server, an administrator runs `/bot plan key:…` with the key from their
   receipt. `/bot plan` shows the plan and how many activations are used,
   re-checks the licence with the store, and `remove:true` gives the activation
   back so it can be used in another server.

If a product is renamed in the store, set `LEMONSQUEEZY_VARIANT_PRO` /
`LEMONSQUEEZY_VARIANT_ENTERPRISE` to its variant id: the name is only a
fallback, the id is stable.

---

## 📚 Docs & deep-dives

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How a slash command flows through the code (with diagram) |
| [`docs/deployment.md`](docs/deployment.md) | Hosting, env vars, backups, monitoring |
| [`docs/voice-debug.md`](docs/voice-debug.md) | Voice-XP not paying? Diagnose it here |
| [`docs/handoff.md`](docs/handoff.md) | Maintainer handoff notes (moved from root) |
| [`CHANGELOG.md`](CHANGELOG.md) | Every shipped change, newest first |
| [`PRIVACY.md`](PRIVACY.md) / [`TERMS.md`](TERMS.md) | Privacy policy & terms (also served by `/legal privacy`, `/legal tos`) |
| [`docs/legal/`](docs/legal/README.md) | Retention schedules, the deletion procedure, and every third party the bot talks to |
| [`deleted_features/README.md`](deleted_features/README.md) | Retired features + how to restore one |

<details>
<summary><b>🪙 The Meow Fee (2% on transfers between members)</b></summary>

Moving coins **between members** carries a small fee into **Meow Bank** (a
per-server house account), not burned: **2%**, minimum 1 🪙, maximum 500 🪙.
Charged on `/pay` and successful `/rob` takings — never on your own
`/deposit`/`/withdraw` or earnings. It's charged **on top**, so the recipient
gets exactly what was sent. Admins: `/eco-admin fee` to view,
`/eco-admin fee state:Off` to disable.

</details>

<details>
<summary><b>📈 The market (real prices, no keys)</b></summary>

`/market` trades 72 instruments (31 crypto + 41 stocks/funds) at **live
exchange quotes** (Binance/TradingView, fallbacks behind them) with
**1 USD = 1 coin** — the board reads like a price board. No API keys. If no
source answers, trading **pauses** instead of filling at a stale price.
Subcommands: `board · quote · chart · buy · sell · portfolio · history`.
Fills are 1:1 with a 0.25% "Meow Treat" fee (min 1 coin, itemized). Trading is
**role-gated** (`MARKET_ROLE_IDS`); every instrument also has a live page at
`<dashboard>/market/<SYMBOL>`. Limits: 100–250,000 coins/fill, 1M buyback/day,
5s cooldown, 3-minute quote freshness.

</details>

<details>
<summary><b>🔐 Who may run what (permissions)</b></summary>

Every command's audience is declared **once** in `utils/permissions.py`
(`COMMAND_ACCESS`: everyone, mod/ban/kick/messages/channels/roles/guild,
admin, server owner, record author, market role). Enforcement stays in the
command checks; visibility (`default_member_permissions`) is stamped before
publishing so members only *see* what they can run. `tests/test_permissions.py`
holds the table and the real commands together — add a command, declare it.

</details>

<details>
<summary><b>➕ Adding or retiring a feature</b></summary>

Every feature is described **once** in `utils/features.py`. To add one:

1. Add a `Feature(...)` entry (id, name, icon, `gate_commands`, `commands`, `cog`).
2. Gate its commands with
   `if not self.bot.is_module_enabled(interaction.guild_id, "<id>"): return`.
3. Run the tests — they fail on ungated commands, stale gates, or help-menu drift.

To retire: delete the entry, move the cog to `deleted_features/`. Nothing else
refers to it by name. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

</details>

---

## 🧪 Tests

```bash
python -m unittest discover -s tests -t .
```

600+ tests, stdlib only, no network: level math, feature-registry drift,
permissions vs. real command checks, atomic money/rob/fee transactions,
market parsing + staleness + races, backup round-trips + integrity checks, legal-doc
rendering inside Discord's embed limits. CI runs them on every push/PR (and on
demand), and fails the build if fewer than 740 tests actually run or more than 20
skip — a suite that quietly stops collecting tests cannot pass as green.

---

## 🆘 Troubleshooting

| Symptom | Fix |
|---|---|
| Bot offline but logs say "ready" | Wait 1–2 min — global commands register slowly |
| No slash commands at all | Re-invite with the `applications.commands` scope |
| "Missing Permissions" on ban/kick/mute | Drag the bot's role **above** the target roles |
| Ticket button creates nothing | Bot needs `Manage Channels` — see below |
| Free host sleeps/stops | Expected on free tiers — see `docs/deployment.md` |

Voice rewards misbehaving? → [`docs/voice-debug.md`](docs/voice-debug.md).

### What the invite asks for

There are **two invite links**, and every page that offers one offers both:

| Link | Asks for |
|---|---|
| The default "Add" button | **Administrator** — one permission, and nothing else |
| "Add with limited permissions" | the explicit list below |

The default asks for `Administrator` so that **no feature can fail later** while
you work out which permission it wanted. The explicit list is always one
permission behind the code: every feature added after it was written reaches a
server as `Missing Permissions`, which reads as a broken bot. One bit cannot fall
behind.

It is also the larger grant, and the cost is worth stating rather than leaving in
a comment: an Administrator bot can do **anything** in a server that adds it, so
a fault in one command is unbounded — it reaches the whole server rather than the
one thing that command does. If you would rather not hand that over, the second
link asks only for what the bot uses, and each entry names the call site that
needs it (`utils/discord_auth.py`):

| Permission | Needed by |
|---|---|
| View Channels, Send Messages, Embed Links, Attach Files, Read Message History | every command that answers, including the cards |
| Add Reactions, Use External Emojis | the starboard |
| Kick Members, Ban Members, Moderate Members | `/kick`, `/ban`, `/mute`, and automod's timeout |
| Manage Messages | `/purge` |
| Manage Channels | `/lock`, `/unlock`, and ticket channels |
| Manage Roles | level rewards and `/defaultrole` |
| Change Nickname | a server's own name and picture for the bot |
| View Audit Log | telling you who added the bot |
| Create Public Threads, Send Messages in Threads | the ticket panel and the `/help` panel |

That list deliberately leaves out `Mention Everyone` (announcements ping a role,
which needs no permission), `Connect` (nothing joins voice — the tracker reads
state changes), Manage Server, Manage Webhooks, Create Invite, and the member
move/mute/deafen bits. A grant nothing uses is still a grant, and it is what a
leaked token reaches for first.

> On the limited link, if a feature reports "Missing Permissions", grant that one
> permission to the bot's role. Existing servers keep whatever they already
> approved — changing an invite link never alters a bot that is already in.

---

## 🔐 Security

- Secrets live in `.env` / host env vars only. Full policy: [`SECURITY.md`](SECURITY.md).
- No default dashboard passkey — unset `ADMIN_PASSKEY` disables passkey login
  (use the ⚡ Open Dashboard button on `/help`); always set `FLASK_SECRET_KEY`
  in production.
- Cloud backups live in a **private** Supabase bucket (`bot.db` + 5 hourly snapshots).

---

## 🤝 Contributing

New contributor? [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through setup,
where things live, and the 4-step recipe for adding a feature. Small focused
PRs welcome — please run the tests first.

MIT licensed — see [`LICENSE`](LICENSE).
