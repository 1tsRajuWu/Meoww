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
  passkey login is disabled and admins sign in with the one-time `/dashboard`
  link from Discord. Set a strong, unique `ADMIN_PASSKEY` to enable passkey login.
- Without `FLASK_SECRET_KEY`, sessions reset on every restart. Always set it in
  production.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, contact the
repository owner directly (see the profile behind the repo URL in `cogs/legal.py`
`REPOSITORY`) with a description of the issue and steps to reproduce. You should
receive an acknowledgement, and we will coordinate a fix before any public
disclosure.
