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
3. **Invite it**: OAuth2 → URL Generator → scopes `bot` +
   `applications.commands` → permission `Administrator` → open the URL.
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
├── templates/             ← dashboard HTML pages
├── assets/                ← fonts + sounds the bot ships with
├── scripts/               ← helper tools (avatar/banner, dev runner, preview)
├── docs/                  ← deployment, voice-debug + architecture guides
├── tests/                 ← 600+ unit tests (stdlib only, no network)
├── deleted_features/      ← retired features, kept but never imported
└── .github/               ← CI, issue forms, PR template
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
   → permission `Administrator` → open the generated URL to invite the bot.

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

Run `/dashboard` in your server → you get a **single-use sign-in link**
(expires in 15 minutes) to the web admin panel: toggle modules in real time,
compose announcements as the bot, and open live market charts. No separate
account — Discord itself is the login.

---

## 📚 Docs & deep-dives

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How a slash command flows through the code (with diagram) |
| [`docs/deployment.md`](docs/deployment.md) | Hosting, env vars, backups, monitoring |
| [`docs/voice-debug.md`](docs/voice-debug.md) | Voice-XP not paying? Diagnose it here |
| [`docs/handoff.md`](docs/handoff.md) | Maintainer handoff notes (moved from root) |
| [`CHANGELOG.md`](CHANGELOG.md) | Every shipped change, newest first |
| [`PRIVACY.md`](PRIVACY.md) / [`TERMS.md`](TERMS.md) | Privacy policy & terms (also served by `/privacy`, `/tos`) |
| [`deleted_features/README.md`](deleted_features/README.md) | Retired features + how to restore one |

<details>
<summary><b>🪙 The Meow Fee (2% on transfers between members)</b></summary>

Moving coins **between members** carries a small fee into **Meow Bank** (a
per-server house account), not burned: **2%**, minimum 1 🪙, maximum 500 🪙.
Charged on `/pay` and successful `/rob` takings — never on your own
`/deposit`/`/withdraw` or earnings. It's charged **on top**, so the recipient
gets exactly what was sent. Admins: `/meowfee` to view, `/meowfee state:Off`
to disable.

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
| Ticket button creates nothing | Bot needs `Manage Channels` (in Administrator) |
| Free host sleeps/stops | Expected on free tiers — see `docs/deployment.md` |

Voice rewards misbehaving? → [`docs/voice-debug.md`](docs/voice-debug.md).

> Once everything works, you can drop the bot's `Administrator` permission to
> exactly what it uses: Ban/Kick Members, Moderate Members, Manage
> Channels/Roles/Messages/Guild, Send Messages, Read History, Add Reactions.

---

## 🔐 Security

- Secrets live in `.env` / host env vars only. Full policy: [`SECURITY.md`](SECURITY.md).
- No default dashboard passkey — unset `ADMIN_PASSKEY` disables passkey login
  (use `/dashboard` one-time links); always set `FLASK_SECRET_KEY` in production.
- Cloud backups live in a **private** Supabase bucket (`bot.db` + 5 hourly snapshots).

---

## 🤝 Contributing

New contributor? [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through setup,
where things live, and the 4-step recipe for adding a feature. Small focused
PRs welcome — please run the tests first.

MIT licensed — see [`LICENSE`](LICENSE).
