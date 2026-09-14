# Discord Bot — Full Setup Guide (Copy-Paste Friendly)

This bot includes: moderation (ban/kick/mute/warn/purge), a full ticket system,
server control (lockdown/slowmode/roles/announcements), leveling & XP, economy &
casino games, a starboard, social/animal roleplay, and basic auto-mod (spam +
invite link filtering).

> Some features (Music, TempVoice, Welcome, Color Studio, Giveaways, Reaction
> Roles) were retired and moved to `deleted_features/` — see that folder's
> README to restore one.

Follow these steps in order. Don't skip the permission steps — they're the
most common reason a bot "doesn't work."

---

## Step 1 — Create the bot on Discord's Developer Portal

1. Go to https://discord.com/developers/applications
2. Click **New Application**, name it, click **Create**.
3. In the left sidebar, click **Bot**.
4. Click **Reset Token** → copy the token that appears. **Save it somewhere
   safe — you can only see it once.** This is your `DISCORD_TOKEN`.
5. On the same Bot page, scroll to **Privileged Gateway Intents** and turn ON:
   - **Server Members Intent**
   - **Message Content Intent**

   Then set these in your `.env` (and in your host's environment variables) so
   the bot actually *requests* them:
   ```
   ENABLE_MESSAGE_CONTENT_INTENT=1
   ENABLE_MEMBERS_INTENT=1
   ```
   They are off by default because requesting an intent the portal hasn't
   enabled makes Discord refuse the whole connection. Without them the bot
   still runs, but message text is invisible — auto-mod cannot filter invite
   links and XP is awarded at the baseline rate.
6. Go to **OAuth2 → URL Generator** in the sidebar.
   - Under **Scopes**, check `bot` and `applications.commands`.
   - Under **Bot Permissions**, check `Administrator` (simplest for an
     all-in-one bot — you can lock this down later once it's working).
7. Copy the generated URL at the bottom, paste it into your browser, and
   invite the bot to your server.

---

## Step 2 — Get the files onto your computer

Download the files I've provided and keep this folder structure:

```
discord-bot/
├── bot.py
├── keep_alive.py
├── requirements.txt
├── Procfile
├── .env.example
├── deleted_features/        # archived features — not loaded by the bot
└── cogs/
    ├── moderation.py
    ├── tickets.py
    ├── server_control.py
    ├── automod.py
    ├── economy.py
    ├── leveling.py
    └── help.py
```

Rename `.env.example` to `.env` and paste in your token:

```
DISCORD_TOKEN=your_actual_token_here
```

**Never share this file or post it publicly.** If your token ever leaks,
go back to the Developer Portal and hit **Reset Token** immediately.

---

## Step 3 — Deploy for free 24/7 hosting

You picked cloud free-tier hosting. Here are your two realistic options —
**Railway is faster to set up**, **Oracle Cloud is more permanently free**.

### Option A: Railway (easiest, but free usage is limited/trial-based)

1. Create a GitHub account if you don't have one, and create a new
   repository (e.g. `my-discord-bot`).
2. Upload all the bot files to that repository (GitHub's web uploader
   works — drag and drop the files/folders).
3. Go to https://railway.app → sign in with GitHub.
4. Click **New Project → Deploy from GitHub repo** → select your repo.
5. Once it's created, click on the service → **Variables** tab → add:
   - `DISCORD_TOKEN` = your token
6. Railway should auto-detect Python and use the `Procfile`. If it doesn't
   start automatically, go to **Settings → Deploy** and set the start
   command to `python bot.py`.
7. Check the **Deployments** tab logs — you should see `Logged in as
   YourBot#1234` and `Bot is online and ready.`

⚠️ Railway's free tier has changed over time and may require a linked card
or limited monthly hours even on "free" plans. If it asks for payment
info or stops after a trial, switch to Option B.

### Option B: Oracle Cloud Free Tier (more work, but genuinely free forever)

1. Sign up at https://www.oracle.com/cloud/free/ (requires a card for
   verification but the "Always Free" tier doesn't charge it for eligible
   resources).
2. Create a free **Compute Instance** (choose an "Always Free eligible"
   shape, Ubuntu image).
3. Connect to it via SSH (Oracle gives you the exact command after setup).
4. Once connected, run:
   ```bash
   sudo apt update && sudo apt install python3-pip python3-venv git -y
   git clone https://github.com/YOUR_USERNAME/my-discord-bot.git
   cd my-discord-bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   nano .env
   ```
   Paste `DISCORD_TOKEN=your_token_here` into the nano editor, then press
   `Ctrl+O`, Enter, `Ctrl+X` to save and exit.
5. Run it in the background so it survives you closing the terminal:
   ```bash
   sudo apt install tmux -y
   tmux new -s bot
   python bot.py
   ```
   Then press `Ctrl+B` then `D` to detach — the bot keeps running. To
   check on it later: `tmux attach -t bot`.

---

## Step 4 — Set up the ticket system in your server

1. In Discord, create a role called exactly **Staff** (this is optional
   but recommended — staff automatically get access to all tickets).
2. In whatever channel you want the ticket panel to appear, run:
   ```
   /ticket-setup
   ```
3. A "Need Help?" embed with a button appears. Anyone who clicks it gets
   a private ticket channel.

---

## Step 5 — Try the rest of the commands

Type `/` in any channel and Discord will show you the full list, or run
`/help` for a categorized summary. A few to try first:

- `/rank` — shows your XP level card
- `/lockdown` — locks the current channel
- `/ban @someone spamming` — bans with a reason
- `/automod-antispam enabled:true` — turns on spam auto-mute (on by default)

---

## The Meow Fee

Moving coins **between members** carries a small fee, and it goes to **Meow
Bank** — a per-server house account — rather than being burned. That is the
whole reason it is small: the coins are still in the server, so there is no
inflation argument for making it bite, only the mild cost of passing coins to
somebody else.

| | |
|---|---|
| **Rate** | 2% of the transfer |
| **Minimum** | 1 🪙 — a fee that rounds away to nothing is not a fee |
| **Maximum** | 500 🪙 — "small" has to keep meaning small as amounts grow |
| **Charged on** | `/pay`, and the takings from a successful `/rob` |
| **Not charged on** | your own `/deposit` and `/withdraw`, or anything you earn |

The fee is charged **on top of** what the sender promised, so the recipient
receives exactly the amount they were sent and never a coin less. On a heist it
comes out of the robber's proceeds, never the victim's, so what the victim loses
does not depend on whether the fee is on.

The coins leave the sender's wallet and reach the bank **inside one
transaction**, so a transfer can never be half-done — and every card shows the
fee as its own line rather than hiding it in the amount.

An administrator can see the current state and the bank's balance with
`/meowfee`, and turn it off with `/meowfee state:Off`. Turning it off moves the
exact amount and banks nothing, but it does not empty Meow Bank.

---

## The market (real prices, no keys)

`/market` trades real assets with server coins. The prices are **live exchange
quotes** — crypto from Binance, stocks from TradingView — with Kraken, Coinbase
and CNBC behind them for hosts the first source blocks. None of them needs an API
key, and none of the numbers is generated by the bot: if no source can be
reached, the board says so and **trading pauses** rather than filling an order at
a stale or invented price.

There are **72 instruments**: 31 coins (the majors, the layer-ones, a couple of
memes) and 41 shares and funds, listed with their own ticker so what you type is
what you meant. **1 USD = 1 coin**, so a price of `76,718` 🪙 *is* the dollar
price — the board reads like a price board rather than a conversion puzzle.

```
/market board                 live prices, 24h change, your holdings
/market quote BTC             one instrument, both sides of the book, a 24h sparkline
/market chart BTC week        real candles, with your average cost drawn on them
/market buy BTC 25000         spend coins
/market sell BTC all          close a position (also: a quantity, or 50%)
/market portfolio             what you hold, what it cost, unrealised vs realised profit
/market history               your recent fills and your all-time realised profit
```

### Charts

`/market chart` draws real open/high/low/close — crypto from Binance's klines
(Kraken when a host is refused), stocks from Yahoo's chart endpoint with Nasdaq
behind it — as candlesticks, with the axis in coins, a dashed current-price line,
and **your average cost as a labelled yellow line**. Profit and loss is then
geometry rather than a number beside a picture: if your line sits above the
price, you are down, and the card says the same thing in coins and percent.

### The live chart page

An image cannot be zoomed, hovered or asked a question, so every instrument also
has a **live interactive page** at `<dashboard>/market/<SYMBOL>` — linked from
`/market chart` and from the board. It polls the bot's own snapshot every 8
seconds, so the price on it is the price you would fill at, with its age and its
source shown. The chart is drawn on a canvas with a **crosshair** that reads out
the candle under the pointer, period buttons (24 hours / 7 days / 30 days), a
symbol picker over the whole catalog, a watchlist, and a **what-if calculator**
that shows what a given spend would buy *after* the treat fee. It serves public
market data and nothing else — no member, no balance, no position — which is why
it needs no login.

When no source publishes history for an instrument, the chart falls back to what
the bot has observed itself — every refresh records the prices it saw — and draws
that as a line, labelled as observations. Nothing is ever drawn that the data
does not contain: too many points becomes a line rather than a smear, a flat
series is padded rather than scaled by zero, and a symbol with no history says so
instead of showing an empty chart.

### Reading profit and loss

`/market portfolio` deliberately reports it in two halves, because they are two
different things:

- **Unrealised** — what your open positions are worth at the sell price against
  what they cost. It is a paper number until you sell.
- **Realised** — what selling has actually banked, across how many sales, and how
  many of those were profitable.
- **Total** — both together, and the card's colour follows this.

### Who may trade

`/market` is **role-gated**: one role, and nobody else. The default role id is
compiled in (`utils.market.DEFAULT_ACCESS_ROLE_IDS`) and `MARKET_ROLE_IDS` — a
comma-separated list — overrides it, so a server can authorise a second role
without a code change. A member either holds one of those roles or is refused
before the command does any work; administrators are not an exception, and a
server that does not have the role at all is told exactly which id is missing
rather than silently letting everyone through. `market.access_problem` is a pure
function, so the rule is tested without a Discord client.

### Trading

- **1 USD = 1 coin, and fills happen at the observed price.** There is no spread
  and no haircut, so a real trader's percentage return is reproduced exactly: an
  asset that moved +10% in dollars moved +10% in coins.
- **The only charge is the Meow Treat fee 🐾 — 0.25%, minimum 1 coin, itemised on
  every receipt, and burned.** It is a **named fee, not a spread**: you can see it
  as its own line (`Meow Treat fee − 25 🪙`) instead of finding it quietly inside
  the price you paid. It is charged on top of a buy, taken out of a sell, and
  never appears twice. A round trip at an unchanged price therefore costs the two
  treats and nothing else.
- **Profit and loss is real, in both directions.** A gain is the asset's gain and
  a loss is the asset's loss, printed on the fill, in the portfolio and in
  `/market history`.
- **Trades are atomic.** A buy debits conditionally and writes the position in
  one transaction, so two simultaneous orders can never share one wallet; a sell
  checks the holding and the rolling 24-hour buyback ceiling in the same
  transaction, so a position cannot be sold twice.
- **Limits:** 100–250,000 coins per fill, 1,000,000 coins bought back from one
  member per rolling day, a five-second cooldown between fills, and a quote more
  than three minutes old cannot be traded against at all.
- **It survives a restart.** The last board is stored in the database, so the
  market shows real prices (with their age) immediately after a redeploy —
  trading still refuses anything past the freshness gate.

The whole system is one top-level command with seven subcommands on purpose:
Discord caps an application at 100 global commands, and a command per action
would have spent seven of them. `tests/test_market.py` fails if the group grows
past seven subcommands or gains a second top-level name.

---

## Who may run what

Every command's audience is declared **once**, in `utils/permissions.py`, for a
specific reason: a permission model spread over forty decorators in thirteen
files cannot be reviewed, and it fails silently — a new command ships with no
gate at all, or an admin-only command quietly loses its check.

`COMMAND_ACCESS` maps each command (a name, or `group sub`) to one of twelve
audiences: `EVERYONE`, `MODERATE`, `BAN`, `KICK`, `MESSAGES`, `CHANNELS`,
`ROLES`, `GUILD`, `ADMIN`, `OWNER` (the server owner, checked in the callback),
`SUBMITTER` (the author of a record) and `MARKET` (the market's own role).

Two things follow from the table, and they are deliberately separate:

* **Enforcement** stays where Discord and discord.py put it —
  `@app_commands.checks.has_permissions(...)`, or a guard in the callback for the
  three audiences Discord has no permission bit for.
* **Visibility** is stamped before publishing:
  `apply_declared_permissions(bot.tree)` sets each command's
  `default_member_permissions` from the table, so a command is *hidden* from the
  members who cannot use it rather than offered and then refused. (Discord's
  picker cannot express "the members holding role X", which is why the market is
  visible to everyone and refuses at use time instead.)

`tests/test_permissions.py` loads the real cogs and holds the two together: it
fails if a command has no declared audience, if a declaration outlives its
command, and — by calling each command's own checks with a fake interaction
holding only the declared permission — if a command refuses its audience or runs
for someone with no permissions at all.

Roles are a second, quieter escalation path, so `role_grant_problem` guards
`/addrole`, `/removerole` and `/addlevelrole`: `@everyone` and managed roles are
refused outright, a role at or above the actor's own highest role is refused, and
a role carrying Administrator (or Manage Server/Roles/Channels/Webhooks, Ban,
Kick, Mention Everyone) can only be handed out by an administrator. A moderator
promoting another moderator to Staff still works — that is delegation, not
escalation.

---

## Adding or removing a feature

Every feature is described **once**, in `utils/features.py`. The slash-command
gates, the dashboard cards, the default on/off state, the help-menu structure and
the list of cogs to load are all *derived* from that registry — so adding a
feature is one entry, not six edits in six files.

1. Add a `Feature(...)` to `FEATURES` with its id, name, icon, category,
   description, `gate_commands`, `commands` and `cog`.
2. Gate its command with
   `if not self.bot.is_module_enabled(interaction.guild_id, "<id>"): return`.
3. Run the tests (below). They fail if a command is left ungated, if a gate
   names a command that doesn't exist, or if the help menu and the registry
   disagree.

To retire a feature, delete its entry and move the cog into `deleted_features/`.
Nothing else refers to it by name.

### Running the tests

```bash
python -m unittest discover -s tests -t .
```

They cover the level math, the feature registry (including command-tree drift),
the permission table against every command's real checks, module-state
persistence and concurrency, backup encryption and integrity
checks, atomic money transactions, the market (provider parsing against captured
live payloads, staleness gating, races on one wallet or one position, the role
gate), the legal documents rendering whole inside Discord's embed limits, and the
Components V2 text budgets. Standard library only — no extra dependencies to
install, and the network is never touched: every provider payload is canned.

---

## Troubleshooting

**Bot shows offline in Discord even though logs say "ready"**
Wait 1-2 minutes after first startup — slash commands take a moment to
register globally.

**Slash commands don't show up at all**
Make sure you invited the bot with the `applications.commands` scope
(Step 1.6). If you already invited it without that scope, redo the OAuth2
URL generation and re-invite it.

**"Missing Permissions" errors on ban/kick/mute**
Your bot's role needs to be **above** the roles it's trying to moderate,
in Server Settings → Roles. Drag the bot's role near the top.

**Ticket button doesn't create a channel**
The bot needs `Manage Channels` permission (included in Administrator).

**Railway app "sleeps" or stops**
This is the tradeoff of free tiers — see Option B (Oracle) for something
closer to true 24/7 uptime, or consider a $5/month VPS later if this
becomes a real project.

---

## A note on "Administrator" permission

Giving the bot Administrator makes setup easy, but it means anyone who
somehow got control of the bot's token could do serious damage to your
server. Once everything works, you can go to Server Settings → your
bot's role → and switch to specific permissions instead: `Ban Members`,
`Kick Members`, `Moderate Members`, `Manage Channels`, `Manage Roles`,
`Manage Messages`, `Manage Guild`, `Send Messages`, `Read Message History`,
`Add Reactions`. That's everything this bot actually uses.

---

## Security

- **Never commit secrets.** `DISCORD_TOKEN`, `FLASK_SECRET_KEY`, `ADMIN_PASSKEY`,
  `GITHUB_BACKUP_TOKEN`, and `GIST_BACKUP_ID` all live in `.env` (git-ignored)
  or in your host's environment variables.
- The web dashboard requires `FLASK_SECRET_KEY` to be set; without it, a random
  key is used per process, so everyone is signed out on every restart.
- **The dashboard has no default passkey.** If `ADMIN_PASSKEY` is unset, passkey
  login and the passkey-authenticated endpoints (`/api/set-module`,
  `/api/live-logs`, `/api/voice-debug`) are disabled; sign in with the one-time
  link from `/dashboard` in Discord instead. Set `ADMIN_PASSKEY` to a strong,
  unique value to enable passkey login.
- Cloud backup is **disabled** unless both `GITHUB_BACKUP_TOKEN` and
  `GIST_BACKUP_ID` are set. Keep the backup Gist *secret* anyway, as defence in
  depth rather than as the only protection.
- Backups are **always encrypted** before they leave the machine
  (XSalsa20-Poly1305, with a SHA-256 integrity check re-verified on the way
  back). Plaintext is never uploaded, so a leaked Gist URL exposes no wallet,
  warning or moderation case. The key is `BACKUP_ENCRYPTION_KEY` when you set
  one, and otherwise one derived from `GITHUB_BACKUP_TOKEN` — which never reaches
  the Gist — so encryption works out of the box.
- `BACKUP_ENCRYPTION_KEY` is therefore **optional**, for operators who want to
  rotate the key independently of the token:
  `python -c "from utils.cloud_sync import generate_key; print(generate_key())"`.
  Use the *same* token and key everywhere the bot runs: a snapshot is only
  readable by a bot holding the key that wrote it.
- If a backup cannot be decrypted — a changed key, a rotated token — the bot
  **stops uploading** and says so, instead of overwriting the snapshot that still
  holds everyone's progress. Put the key back to resume; the old file is intact.
- Recovering access after rotating `GITHUB_BACKUP_TOKEN`:
  `python -c "from utils.cloud_sync import base64_key_for_token;
  print(base64_key_for_token('THE_OLD_TOKEN'))"` prints the value to set as
  `BACKUP_ENCRYPTION_KEY`, which makes the snapshots written under that token
  readable again.
- If a token is ever exposed in a commit, **rotate/revoke it immediately** in the
  relevant provider's settings — deleting the file does not remove it from git
  history.

