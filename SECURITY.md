# Security Policy

## Secrets

- **Never commit secrets.** `DISCORD_TOKEN`, `FLASK_SECRET_KEY`, `ADMIN_PASSKEY`,
  `SUPABASE_URL`, and `SUPABASE_SERVICE_KEY` live in `.env` (git-ignored) or in
  your host's environment variables — never in code, issues, or screenshots.
- If a token ever leaks (commit, screenshot, log paste), **rotate it immediately**:
  Discord Developer Portal → Bot → Reset Token, and similarly for any other provider.
  Deleting the file does not remove it from git history.

## Dashboard access

- The web dashboard has **no default passkey**. If `ADMIN_PASSKEY` is unset,
  passkey login is disabled and admins sign in with the one-time ⚡ Open
  Dashboard link on `/help` in Discord. Set a strong, unique `ADMIN_PASSKEY` to
  enable passkey login.
- Without `FLASK_SECRET_KEY`, sessions reset on every restart. Always set it in
  production.

## Backups hold the whole database

The off-host backup in Supabase Storage is a raw SQLite file in a **private**
bucket: every read and write needs `SUPABASE_SERVICE_KEY`, and nothing is
encrypted on top, so that key is the entire database's password. Keep it in the
host's environment only, and treat anyone with bucket access as someone who can
read every wallet, warning and transcript. `docs/legal/sub-processors.md` lists
every service the bot talks to and what each one receives.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, contact the
repository owner directly (see the profile behind the repo URL in `cogs/legal.py`
`REPOSITORY`) with a description of the issue and steps to reproduce. You should
receive an acknowledgement, and we will coordinate a fix before any public
disclosure.

For a bug that is not a security issue, `/bot bug` files it as a report in the
server's `#bug` channel (the operator's own server when the server has none) — no
ticket, nothing to claim, and no need to find a maintainer first.
