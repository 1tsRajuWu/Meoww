# Changelog

## Unreleased
- **Module toggles actually stick**: The web dashboard now writes the change to the database *before* updating memory and reports a real failure instead of silently claiming success, and every toggle triggers an immediate cloud backup so a restart can't restore an older snapshot and revert it.
- **Fixed modules running while disabled, or not running while enabled**: SQLite now runs in WAL mode with a 15s busy timeout, ending the lock contention between the web thread and the bot thread. Previously a failed read made `is_module_enabled` fall back to `True` (so a disabled module kept running) and a failed write silently dropped the toggle. Reads and writes now fail loudly and retry instead of silently guessing.
- **Shared module defaults**: `DEFAULT_DISABLED_MODULES` in `utils/db.py` is the single source of truth for default state, used by both the bot and the dashboard so they can never disagree. **Giveaway, Color Studio & Roles, and TempVoice now default to OFF** (alongside Welcome). A one-time migration applies this to guilds that had an old saved value.
- **Disabled means disabled**: the giveaway background loop and entry button, and the color palette dropdown, now honour their module toggle (view callbacks previously bypassed the cog checks).
- **Components V2 message system**: Added a full Components V2 design system to `utils/style.py` — semantic cards (`success`/`error`/`info`/`warn`), a rich `card()` builder with fields, thumbnail, media gallery and footer, `progress_bar()` for XP/cooldowns, and an `embed_to_layout()` converter that upgrades any legacy embed into a container card. Text is clamped to Discord's 4000-character / 40-component V2 limits, and every helper falls back to the original embed if conversion or sending fails, so no command can break.
- **Migrated 98 messages to Components V2** across 13 cogs (economy, games, moderation, music, leveling, fun, social, server_control, starboard, colors, legal, tempvoice, tickets) via `send_card`/`edit_card`. Interactive panels that carry button/menu views are intentionally left on the legacy path for now, since merging a legacy View into a layout would drop its timeout and `interaction_check` guards.
- **Security — credentials**: Removed a hardcoded (obfuscated) GitHub token and the default Gist ID from `utils/cloud_sync.py`. Cloud backup now requires `GITHUB_BACKUP_TOKEN` and `GIST_BACKUP_ID` from the environment, and warns if the backup Gist is public. **The exposed token must be revoked and reissued.**
- **Security — login rate limiting**: `X-Forwarded-For` is now only trusted when actually behind a proxy, closing a rate-limit bypass that allowed unlimited passkey guesses.
- **Security — sign-out**: `/logout` now requires a CSRF-validated POST instead of a GET link that any site could trigger.
- **Dashboard UX**: Toasts show human-readable module names, and an expired session now redirects back to sign-in instead of silently failing.

## 1.0.0
- **Database Persistence**: Migrated warnings, reaction roles, and welcome configurations from in-memory and flat JSON files to a robust SQLite database using `aiosqlite`.
- **Global Error Handling**: Added a global error handler to safely catch and log unhandled exceptions and permissions issues without leaking stack traces or crashing the bot.
- **Server and User Info**: Implemented `/serverinfo` and `/userinfo` commands for quick utility usage.
- **Mod Logging**: Added a `/modlog` command and integrated an automatic moderation logging system for bans, kicks, timeouts, warnings, and unbans.
- **Rate Limiting**: Added `cooldown` checks to abuse-prone commands like `/ban`, `/kick`, and `/purge` to mitigate rapid request spamming.
- **Robust Configuration**: Cleaned up dynamic configurations (AutoMod features) to sync properly between memory caching and SQLite storage.
- **Dependency Upgrades**: Added `aiosqlite` to `requirements.txt`.
