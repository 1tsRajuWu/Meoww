# Handoff Document

## What Changed and Why
I have successfully audited and hardened the bot to ensure it acts as a robust, production-ready Discord moderation and utility service. 
Please refer to the [CHANGELOG.md](./CHANGELOG.md) for a full breakdown, but the core highlights include:
- **Database Backend**: Ported all flat JSON config and memory-based storage (warnings, server configuration) into SQLite (`bot.db`) for true restart persistence.
- **Improved Error Handling**: Introduced a global error handler so invalid commands log gracefully rather than crash the thread or spam chat with Python tracebacks.
- **New Utilities**: Added `/serverinfo`, `/userinfo`, and an optional moderation logging system (`/modlog`) to keep track of staff actions.
- **Rate-limiting**: Mitigated abuse vectors by applying cooldowns to bulk/destructive commands (e.g. `/purge`).

## Where things live

| Concern | File |
| --- | --- |
| Every feature (id, name, icon, commands, default state, cog) | `utils/features.py` |
| Module on/off state — the only cache, reader and writer | `utils/modules.py` |
| SQLite schema, migrations and `run_atomic()` | `utils/db.py` |
| XP/level math and the `MAX_LEVEL` ceiling | `utils/levels.py` |
| Components V2 design system (`send_card`, `card`, semantic helpers) | `utils/style.py` |
| Cloud backup (encrypted + rotated snapshots) | `utils/cloud_sync.py` |
| Web dashboard (Flask, runs in a thread from `bot.py`) | `keep_alive.py` + `templates/` |
| Archived features, kept but never imported | `deleted_features/` |
| Tests | `tests/` — run `python -m unittest discover -s tests -t .` |

**Adding or retiring a feature** is a single edit in `utils/features.py` (plus the
cog itself). The command gates, dashboard cards, help menu and defaults all
follow from it, and the test suite fails if any of them drift apart.

## Remaining Manual Steps (Deployment)
To get this bot fully online, follow these precise steps:

1. **Obtain your Bot Token:**
   - Go to the [Discord Developer Portal](https://discord.com/developers/applications).
   - Select your Application -> Click **Bot** on the left menu.
   - Click **Reset Token** and copy the new token. Do not share this token.
   - On the same page, scroll down and ensure **Server Members Intent** and **Message Content Intent** are toggled **ON**, then save.

2. **Invite the Bot to your Server:**
   - Go to **OAuth2 -> URL Generator**.
   - Under scopes, check `bot` and `applications.commands`.
   - Under permissions, check `Administrator`.
   - Copy the generated URL, paste it into your browser, and authorize the bot into your server.

3. **Deploy on Railway (Recommended):**
   - Create a [GitHub](https://github.com/) repository and push this entire folder's contents (including `bot.py`, `requirements.txt`, `Procfile`, etc.).
   - Log into [Railway.app](https://railway.app/).
   - Click **New Project** -> **Deploy from GitHub repo** and select your newly created repository.
   - Once the service creates, click into it, go to the **Variables** tab, and add a new variable:
     - **Name**: `DISCORD_TOKEN`
     - **Value**: *(Paste the token from Step 1)*
   - Railway will automatically detect the Python environment and the `Procfile`, and deploy the bot. 

## How to Verify
1. **Check the Logs:**
   - In Railway, click on your service and go to the **Deployments** tab. Click **View Logs**.
   - You should see the following lines indicating success:
     ```
     Logged in as YourBotName#1234 (ID: ...)
     Synced X slash command(s).
     Bot is online and ready.
     ```
2. **Verify in Discord:**
   - Go to your Discord server and type `/help` in any channel. 
   - The bot should instantly respond with an embed showing all available commands.
   - Test a utility command like `/serverinfo` to ensure everything is operating smoothly!
