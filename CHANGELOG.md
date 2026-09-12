# Changelog

## 1.0.0
- **Database Persistence**: Migrated warnings, reaction roles, and welcome configurations from in-memory and flat JSON files to a robust SQLite database using `aiosqlite`.
- **Global Error Handling**: Added a global error handler to safely catch and log unhandled exceptions and permissions issues without leaking stack traces or crashing the bot.
- **Server and User Info**: Implemented `/serverinfo` and `/userinfo` commands for quick utility usage.
- **Mod Logging**: Added a `/modlog` command and integrated an automatic moderation logging system for bans, kicks, timeouts, warnings, and unbans.
- **Rate Limiting**: Added `cooldown` checks to abuse-prone commands like `/ban`, `/kick`, and `/purge` to mitigate rapid request spamming.
- **Robust Configuration**: Cleaned up dynamic configurations (AutoMod features) to sync properly between memory caching and SQLite storage.
- **Dependency Upgrades**: Added `aiosqlite` to `requirements.txt`.
