# Scripts 🛠️

Small helper tools. Run them from the **repo root** (`cd` into the folder first).
All of them need `DISCORD_TOKEN` in `.env` — copy `.env.example` to `.env` first.

| Script | What it does | Example |
|---|---|---|
| `scripts/check_avatar.py` | **Read-only.** Prints the bot's current avatar URL. | `python scripts/check_avatar.py` |
| `scripts/update_avatar.py` | **Changes the bot.** Sets its avatar from an image file. | `python scripts/update_avatar.py assets/avatar.png` |
| `scripts/update_banner.py` | **Changes the bot.** Sets its profile banner from an image file. | `python scripts/update_banner.py assets/banner.png` |
| `scripts/run-dev.sh` | Dev loop: runs `bot.py`, restarts it on crash. | `bash scripts/run-dev.sh` |
| `scripts/preview_dashboard.py` | Serves the real dashboard **and** market templates with fake data so you can eyeball UI changes without Discord, at <http://127.0.0.1:8099/> (set `PORT` to move it). | `python scripts/preview_dashboard.py` |
| `scripts/ship.py` | **Deploys the public site and the owner console** to Cloudflare Pages and then checks that both hosts took the commit. Needs `wrangler` logged in (`npx wrangler login`), a clean tree and a pushed `main` — it refuses otherwise, because it stamps each upload with the commit it was built from. | `python scripts/ship.py` |

Notes for newbies:

- `force_sync.py` lives at the **repo root** (not here) because it's a main
  entry point: `python force_sync.py --dry-run` shows which slash commands
  would be re-registered with Discord; drop `--dry-run` to do it.
- `ship.py` is the one script that does **not** need `DISCORD_TOKEN`. It is the
  local twin of `.github/workflows/site.yml` — that workflow cannot run on this
  repository's GitHub account, so this is what actually publishes. It builds
  `_site` and `_owner`, uploads both, and then asks Cloudflare which commit
  production serves; `--dry-run` builds both and stops, and `--only meow-owner`
  ships one host.
- `update_avatar.py` / `update_banner.py` accept a path argument, or
  `AVATAR_PATH` / `BANNER_PATH` env vars, or fall back to `assets/`.
- None of these are imported by the bot — they're standalone.
