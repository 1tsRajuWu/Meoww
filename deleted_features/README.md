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

- `utils/catbox.py`, `utils/file_kiwi.py`, `utils/streamable.py` — the hosts
  `/upload` streamed to (uploads only). `utils/filehost.py` is archived
  alongside them but is only a size-formatting helper (`format_size`) these
  days, not an upload host: the old file.io path is gone and the pinned
  instructions explicitly do not advertise file.io.
- `requirements-archived.txt` — the pip packages only music needed.
- `tests/test_uploads.py` — the upload suite, kept here rather than deleted. It
  imports `cogs.uploads`, so it only runs again once the cog is restored.
- `deleted_features/conftest.py` — tells pytest to collect nothing from this
  folder, so an archived test cannot turn a plain `pytest` into a collection
  error.

> **Note:** `utils/spotify.py` and `utils/card.py` were *not* moved here.
> `card.py` is still imported live by `utils/chart.py` (`font`), and both files
> stay live so `cogs/music.py` remains restorable without hunting history.

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

---

Archived on 2026-09-22, at the owner's request: **the music resolver**, and the
commands that were only ever a front end for it. `/play` turned words into one
specific recording through `yt-dlp`, a ladder of player clients, three providers
(YouTube, SoundCloud, Audius), a second-provider rescue and a Spotify half, with
an outbound proxy and a cookie/PO-token pair as the answers to a platform
refusing the host. Every path in it ended at a media platform, and a platform
that has decided a datacenter address is a robot cannot be argued with from this
side — so the cookies and the token were maintenance with an expiry date rather
than a fix, and the honest feature was the one that never needed them.

| Feature | File | Notes |
|---|---|---|
| The resolver | `utils/media_source.py` | The file as it stood, whole. The **live** `utils/media_source.py` is the stream half of the same name and nothing else — `Track` and the probe (`Probe`, `probe_target`, `self_check`) — because radio still plays. Everything that existed only to turn words into a recording went with the extractor, `Stream` and the ffmpeg proxy helper included: a `Stream` was what a *provider* handed back and the proxy was what carried a signed fetch, and there is neither a provider nor a signature left. Restoring a resolver means restoring *this* file over the live one. |
| Its suite | `tests/test_music_matching.py` | ~87 tests: matching the right recording, the client ladder, the two-phase search, the rescue, the catalogue, the proxy. Verbatim. |
| The resolver half of the player's suite | `tests/test_music_playback.py` | The file as it stood: the rescue cases, seek, loop, the proxy-on-the-fetch case and the `/play` command-surface assertions. The live `tests/test_music.py` keeps what survives (the generation counter, the cut-stream guard, source selection, the permission audit). |
| The refusal sentences | `tests/test_radio_refusals.py` | The two classes that lived in `tests/test_radio.py`: which cause a failure names, and the "the radio still works anyway" hint. Kept because telling a member *"nothing is wrong with your search"* instead of *"the platform refused this host"* over a request that simply had no recording in it is a distinction any future resolver will have to make again. |

Removed from the live tree with them: `/play`, `/music songinfo`, `/music lyrics`,
`/music seek`, `/music loop`, `/music shuffle`; the `blocked` and `rescued` fault
kinds and their cards; the command-surface tests that counted two `/music` roots;
and the seven `YTDL_*` settings in `utils/config_audit.py` (`COOKIES`,
`COOKIES_B64`, `PO_TOKEN`, `VISITOR_DATA`, `SOURCE_ORDER`, `PROXY`,
`PROBE_TARGET` — the last replaced by `STREAM_PROBE_TARGET`, which names a
station). `requirements.txt` lost `yt-dlp`; **`imageio-ffmpeg` stayed**, because
ffmpeg is what decodes a station's MP3/AAC and encodes Opus for Discord — it is
the radio player, not the extractor.

**Nothing needs moving to restore it beyond the four files above**: `yt-dlp` is
already listed in `requirements-archived.txt`, and `utils/spotify.py` and
`utils/card.py` were left live on purpose (the now-playing card still draws).

---

Archived on 2026-09-23, at the owner's request: **the radio as it was, replaced by
a rebuild of the same feature** — *"rebuild the whole radio sys from scartch and
remove music and keep radio only"*. What was there worked, but had grown three
centres of gravity: rows, commands, cards and the 24/7 timer in one 1,900-line
cog; a player still carrying queue, seek, loop and filter machinery from the
system it had outlived; and a media module doing two jobs. The surface said
*Music* on every page the bot serves.

The rebuild is four files with one job each — `utils/radio_catalogue.py` is the
data, `utils/radio.py` the policy, `utils/radio_stream.py` the engine and
`cogs/radio.py` the surface — with a table rebuilt from a probed pass and
fourteen commands replaced by three (`/radio`, `/radio stop`, `/radio doctor`).

| Feature | File | Notes |
|---|---|---|
| The catalogue and the surface | `radio_2026_09/cogs_music.py` | The rows, the commands, the cards and the seat timer in one file; its `/music` group is what `/radio` replaces. |
| The player | `radio_2026_09/utils_player.py` | The engine, with the queue, seek, loop, filter and queue-drawing machinery a live stream never needed. `utils/radio_stream.py` is the rebuild: one station, one socket, a *named* retry budget and the cut-stream guard, and nothing queue-shaped. |
| The stream half of the media module | `radio_2026_09/utils_media_source.py` | The module as it stood. The live engine carries its own `Station` and its own probe, so nothing imports this. |
| The public page | `radio_2026_09/templates_music.html` | `/music` is a **301 to `/radio`** now, and `templates/radio.html` is the page. |
| The suites | `radio_2026_09/test_music.py`, `radio_2026_09/test_radio.py` | The old player's mechanics and the old catalogue's rules, verbatim. The live `tests/test_radio.py` is the **new** suite — the same filename because it covers the same feature. |

**Nothing the table needs was deleted**: `imageio-ffmpeg` stays in
`requirements.txt`, because ffmpeg is what plays a station. `utils/spotify.py`
went with it this time — nothing had imported it since the resolver was archived
on 2026-09-22, and a module kept "in case" is a module the next reader has to
re-decide about.

## How to restore a feature

1. Move the file back: `mv deleted_features/cogs/<name>.py cogs/`
2. Re-add `cogs.<name>` to `INITIAL_COGS` in `bot.py`.
3. Re-add its command names to `MODULE_COMMAND_MAP` in `bot.py`.
4. Re-add the dashboard card to `AVAILABLE_MODULES` in `keep_alive.py`.
5. Re-add its help category in `cogs/help.py` (and the home-body bullet).
6. For **radio** as it was before the rebuild (`radio_2026_09/`): move
   `cogs_music.py` back to `cogs/music.py`, `utils_player.py` to
   `utils/player.py`, `templates_music.html` to `templates/music.html` and both
   suites to `tests/`, then `pip install yt-dlp imageio-ffmpeg` (+ a system
   `ffmpeg` binary). It collides with the live `cogs/radio.py` by design: the
   `/music` group and the `/radio` group would both exist, so delete the one you
   are not keeping. For the **resolver** (the 2026-09-22 archive), move
   `deleted_features/utils/media_source.py` back over the live engine's own name
   and move the three test files back, then re-add `yt-dlp` to
   `requirements.txt`.
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
