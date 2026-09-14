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

Archived on 2026-09-14, at the owner's request, after it spent a week not
working end to end: `/upload` handed back an empty file.kiwi folder the member
had to fill in themselves, and the host's own preview of what did arrive never
rendered, so "the upload finished but nothing shows" was the normal outcome
rather than a bug to fix.

| Feature | File | Notes |
|---|---|---|
| Big-file uploads (`/upload`, `/upload-big`, `/pin-upload-info`) | `cogs/uploads.py` | Needed an external host (catbox / Streamable / file.kiwi), a pinned instructions post and a bound `#uploads` channel. Members are back to Discord's own attachment limit. |

Supporting files that only these features used were archived too:

- `utils/spotify.py` — Spotify link resolver (music only).
- `utils/card.py` — `generate_now_playing_card()` image renderer (music only).
- `utils/catbox.py`, `utils/file_kiwi.py`, `utils/filehost.py`,
  `utils/streamable.py` — the hosts `/upload` streamed to (uploads only).
- `requirements-archived.txt` — the pip packages only music needed.
- `tests/test_uploads.py` — the upload suite, kept here rather than deleted. It
  imports `cogs.uploads`, so it only runs again once the cog is restored.
- `deleted_features/conftest.py` — tells pytest to collect nothing from this
  folder, so an archived test cannot turn a plain `pytest` into a collection
  error.

## What was removed from the live bot

- `bot.py` — cog entries in `INITIAL_COGS` and their command map entries.
- `keep_alive.py` — the dashboard module cards in `AVAILABLE_MODULES`.
- `cogs/help.py` — help categories, descriptions and the home menu bullets.
- `cogs/bot_config.py` — the music-showcase channel reminder.
- `requirements.txt` — `yt-dlp`, `imageio-ffmpeg`.
- Docs (`README.md`, `PRIVACY.md`, `HANDOFF.md`).

For the uploads removal specifically, the feature registry entry in
`utils/features.py`, the `/upload*` permission declarations in
`utils/permissions.py`, the sticky-post block in `bot.py`, the `#uploads`
binding in `cogs/bot_config.py`'s `/updatedb`, and `UPLOAD_CHANNEL_ID` in
`.env.example` were all removed with it.

**Left in place on purpose:** the SQLite tables and helper functions in
`utils/db.py` (`giveaways`, `giveaway_entries`, `tempvoice_*`, `music_247`,
`reaction_roles`). Removing them would have dropped stored data for zero
benefit, since nothing reads them while the cogs are archived. The
`upload_channel_id` guild setting is kept for the same reason — no live code
reads it now.

## How to restore a feature

1. Move the file back: `mv deleted_features/cogs/<name>.py cogs/`
2. Re-add `cogs.<name>` to `INITIAL_COGS` in `bot.py`.
3. Re-add its command names to `MODULE_COMMAND_MAP` in `bot.py`.
4. Re-add the dashboard card to `AVAILABLE_MODULES` in `keep_alive.py`.
5. Re-add its help category in `cogs/help.py` (and the home-body bullet).
6. For **music** only: `pip install yt-dlp imageio-ffmpeg`, and move
   `deleted_features/utils/spotify.py` + `card.py` back into `utils/`.
7. For **uploads** only: move `catbox.py`, `file_kiwi.py`, `filehost.py` and
   `streamable.py` back into `utils/`, move
   `deleted_features/tests/test_uploads.py` back into `tests/`, re-add the
   `Feature(id="uploads", ...)` entry in `utils/features.py`, the three
   `/upload*` lines in `utils/permissions.py`, and the help category in
   `cogs/help.py`.
8. For **uploads** only, also drop `upload`, `upload-big` and
   `pin-upload-info` from `commands_sync.RETIRED_COMMANDS` in
   `utils/commands_sync.py` — that list is what deleted them from Discord's
   command picker, and it would delete them again the moment they are republished.
