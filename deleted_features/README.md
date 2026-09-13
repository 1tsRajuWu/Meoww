# Deleted Features (archive)

These features were **removed from the running bot** but are kept here intact so
nothing is lost. They are **not imported, not loaded and not registered** — the
bot never sees this folder.

Archived on 2026-09-13 because they were the most fragile / buggy parts of the
bot (voice streaming, join-to-create channels, reaction collectors) and the
maintenance cost outweighed their value.

| Feature | File | Notes |
|---|---|---|
| Music & Radio | `cogs/music.py` | Heaviest file (~1600 lines). Needed `yt-dlp`, `imageio-ffmpeg`, ffmpeg binaries and the Spotify resolver. |
| TempVoice (Join-to-Create) | `cogs/tempvoice.py` | Needed gateway voice-state listeners and DB rows in `tempvoice_*`. |
| Welcome / Goodbye | `cogs/welcome.py` | Member-join messages (intents-dependent). |
| Color Studio & Roles | `cogs/colors.py` | Managed per-member colour roles. |
| Giveaway System | `cogs/giveaway.py` | Background countdown task + persistent entry buttons. |
| Reaction Roles | `cogs/reaction_roles.py` | Raw reaction listeners. |

Supporting files that only these features used were archived too:

- `utils/spotify.py` — Spotify link resolver (music only).
- `utils/card.py` — `generate_now_playing_card()` image renderer (music only).
- `requirements-archived.txt` — the pip packages only music needed.

## What was removed from the live bot

- `bot.py` — cog entries in `INITIAL_COGS` and their command map entries.
- `keep_alive.py` — the dashboard module cards in `AVAILABLE_MODULES`.
- `cogs/help.py` — help categories, descriptions and the home menu bullets.
- `cogs/bot_config.py` — the music-showcase channel reminder.
- `requirements.txt` — `yt-dlp`, `imageio-ffmpeg`.
- Docs (`README.md`, `PRIVACY.md`, `HANDOFF.md`).

**Left in place on purpose:** the SQLite tables and helper functions in
`utils/db.py` (`giveaways`, `giveaway_entries`, `tempvoice_*`, `music_247`,
`reaction_roles`). Removing them would have dropped stored data for zero
benefit, since nothing reads them while the cogs are archived.

## How to restore a feature

1. Move the file back: `mv deleted_features/cogs/<name>.py cogs/`
2. Re-add `cogs.<name>` to `INITIAL_COGS` in `bot.py`.
3. Re-add its command names to `MODULE_COMMAND_MAP` in `bot.py`.
4. Re-add the dashboard card to `AVAILABLE_MODULES` in `keep_alive.py`.
5. Re-add its help category in `cogs/help.py` (and the home-body bullet).
6. For **music** only: `pip install yt-dlp imageio-ffmpeg`, and move
   `deleted_features/utils/spotify.py` + `card.py` back into `utils/`.
