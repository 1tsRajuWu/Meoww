# Discord Bot — Full Setup Guide (Copy-Paste Friendly)

This bot includes: moderation (ban/kick/mute/warn/purge), a full ticket system,
server control (lockdown/slowmode/roles/announcements), welcome/goodbye
messages, reaction roles, and basic auto-mod (spam + invite link filtering).

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
├── welcome_config.json
├── reaction_roles.json
└── cogs/
    ├── moderation.py
    ├── tickets.py
    ├── server_control.py
    ├── welcome.py
    ├── reaction_roles.py
    ├── automod.py
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

- `/setwelcome #general` — turns on welcome messages
- `/lockdown` — locks the current channel
- `/ban @someone spamming` — bans with a reason
- `/automod-antispam enabled:true` — turns on spam auto-mute (on by default)

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
- Cloud backup is **disabled** unless both `GITHUB_BACKUP_TOKEN` and
  `GIST_BACKUP_ID` are set. Keep the backup Gist *secret*, since it contains the
  whole database.
- If a token is ever exposed in a commit, **rotate/revoke it immediately** in the
  relevant provider's settings — deleting the file does not remove it from git
  history.

