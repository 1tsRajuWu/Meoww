# Contributing to DaCat Bot 🐾

New here? Perfect — this guide assumes it's your first time touching this repo.

## 1. One-time setup (5 minutes)

```bash
git clone https://github.com/1tsRajOwO/meow-discord-bot.git
cd meow-discord-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your DISCORD_TOKEN into .env
```

Run the bot:

```bash
python bot.py
```

Run the tests (do this before every pull request):

```bash
python -m unittest discover -s tests -t .
```

All tests must pass. They use only the standard library plus the installed
requirements, and they never touch the network or your real `bot.db`.

## 2. How the code is organized

| Path | What it is |
|---|---|
| `bot.py` | The front door: intents, cog loading, command sync, error handling. Start reading here. |
| `keep_alive.py` | The web dashboard (Flask, runs in a thread). Module toggles, message composer, live market pages. |
| `cogs/` | One file per feature (economy, leveling, market, moderation, …). Each file is one Discord "cog". |
| `utils/` | Shared helpers with no Discord dependency where possible (money math, XP math, market prices). |
| `utils/features.py` | **The feature registry — read this second.** Every feature is declared once here. |
| `templates/` | Dashboard HTML pages. |
| `scripts/` | Helper tools (avatar/banner setters, dev runner, dashboard preview). |
| `docs/` | Deployment, voice-debug, and architecture guides. |
| `tests/` | 600+ unit tests. If you change behavior, add or update a test. |
| `deleted_features/` | Retired features, kept intact so nothing is lost. Never imported. |

`docs/architecture.md` has the full picture with a diagram.

## 3. Adding or changing a feature

1. **Declare it once** in `utils/features.py` (`Feature(...)` entry with id, name,
   icon, `gate_commands`, `commands`, `cog`).
2. **Gate its commands**: at the top of each command callback, return early with
   `if not self.bot.is_module_enabled(interaction.guild_id, "<id>"): return`.
3. **Declare its audience** in `utils/permissions.py` (`COMMAND_ACCESS`).
4. **Run the tests.** They fail if a command is ungated, a gate names a command
   that doesn't exist, the help menu disagrees with the registry, or a permission
   declaration is a lie. Trust them — that drift is exactly what they guard.

To **retire** a feature: delete its registry entry and move the cog to
`deleted_features/cogs/`. See `deleted_features/README.md` for restore steps.

## 4. Style rules (enforced by `tests/test_static_health.py`)

- No unused imports, no bare `except:`, no f-strings without placeholders.
- No duplicate top-level definitions in one file.
- Every file in `bot.py`, `keep_alive.py`, `cogs/`, `utils/` must parse and have
  no undefined names. The test tells you the file and line — fix it there.

## 5. Pull requests

- Keep PRs small and focused (one feature or fix).
- Describe **what** changed and **why**, plus how you tested it.
- Never commit secrets (`.env` is git-ignored for a reason — see `SECURITY.md`).
- The CI workflow (`.github/workflows/tests.yml`) runs the full suite on every PR.
