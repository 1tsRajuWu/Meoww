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

Archived on 2026-09-26, at the owner's request: **the voice-chat watch** — *"remove
The voice-chat watch buz its no use"*. It reported a member who writes in a voice
channel's text chat without being placed in that call, which it did honestly; what
it could never do is the thing the feature is named for, because Discord tells a
bot nothing about a channel being *opened*. A watch whose first sentence has to be
"this fires on a message, not on a visit" is a watch that reads as broken to
whoever switches it on, and the reports land in a deployment-wide channel rather
than in the server they are about.

| Feature | File | Notes |
|---|---|---|
| The decision, the two addresses and the seed | `utils/vc_watch.py` | `is_peek` (the whole rule), `status`, `facts`, and `seed_home`, which switched it on in the operator's own server. |
| The listener and the switch | `cogs/moderation.py` | The `on_message` listener, the `/watch` command and its own module-gate branch; the rest of the cog is untouched. |
| The suites | `tests/test_vc_watch.py`, `tests/test_owner_live.py` | The rule with stand-ins, and the console's Live page — which existed to be honest about this one card. |

**What was removed from the live bot**: the `Feature(id="watch", ...)` entry in
`utils/features.py` (so it is no longer a system on any systems page, and
`/watch` is no longer in the tree), `"watch": ADMIN` in `utils/permissions.py`,
the two `Setting(...)` entries in `utils/config_audit.py`, both lines in
`.env.example`, the `seed_home` call in `bot.py`, the `vc_watch` import and the
Live page's `context["watch"]` in `keep_alive.py`, the card in
`templates/owner.html`, the `HELP_MODULES["watch"]` category in `cogs/help.py`,
and the three paragraphs about it in `docs/discord-directory.md` — the listing
said five systems ship off by default and it is four now.

**Stored switch rows are deliberately left alone.** A server that had switched
the watch on keeps a `guild_modules` row naming an unknown module: harmless (the
registry is what reads it, and nothing is left to switch), and it is the state a
restore wants back rather than one it has to rebuild.

Archived on 2026-10-06, at the owner's request: **Decat and Alone Time** — *"it will
go direct to Delete Features ... and also delete alone cmd"*. Between them they
held the last two command slots, which is the other half of why this is a
retirement rather than a rewrite.

**Decat** relayed a question from the bot's host to the operator's own laptop,
where a local model answered it from the live web and the answer came back with
its sources. It worked: the first live question was answered on that laptop in
thirty-one seconds, and the wire had a test that needed no laptop at all. What
finished it was the machine underneath — six gigabytes of weights on a 16 GB
fanless Air that is also running a browser, ~1.9 GB paged out during that first
answer, a wait measured in tens of seconds per question, and failure modes
(*"sry, offline rn"*) that look identical whether the laptop is shut, asleep, or
merely busy with somebody else's afternoon. Retiring it also retires the only
place in this deployment where a question left the host at all, which is a
sentence `TERMS.md` §8 and `PRIVACY.md` can now say without a caveat.

**Alone Time** locked a voice channel to the member holding it, with the room's
*capacity* doing the real work (Discord's Administrator permission ignores channel
overwrites, which the feature said out loud on every card). It shipped switched
off as a single-server beta and never left that server.

| Feature | File | Notes |
|---|---|---|
| Decat, both halves | `cogs/decat.py`, `decat/` | The cog was the only Discord-facing file. Behind it: `decat/protocol.py` (the wire), `copy.py` (every sentence, pinned by laws), `card.py`, `relay.py` (the memory-only queue), `config.py` — and `decat/mac/`, the whole laptop half: agent, Ollama client, prompts, web and image tools, history, the switch page, `doctor`, `install.sh`, `README.md`, `requirements.txt`. |
| Alone Time | `cogs/alonetime.py`, `utils/alonetime.py` | The `/alone` group and the voice sweep, and the ledger behind them. |
| The suites | `tests/test_decat.py`, `tests/test_decat_mac.py`, `tests/test_alonetime.py` | The bot half, the laptop half (including the test that drives the real Flask routes with no Mac), and the lock rules. |

**What was removed from the live bot**: the `Feature(id="decat", ...)` and
`Feature(id="alonetime", ...)` entries in `utils/features.py` (**19 systems → 17**,
**100 commands → 98**), every `alone*` row plus `decat` and `bot decat` in
`utils/permissions.py`, both `HELP_MODULES` categories and their home-body bullets
in `cogs/help.py`, `/bot decat` in `cogs/bot_config.py`, the two Alone Time guards
in `cogs/company.py` (the companion left a room that had just been locked), the
three `/api/decat/*` routes and the `/api/alone-time` door in `keep_alive.py`
together with the `alonetime` import, the ledger reader and the template values
behind them, the "Rooms on Alone Time" panel in `templates/dashboard.html`, the
harness's fixture for it in `scripts/preview_dashboard.py`, the `DECAT_TOKEN` /
`DECAT_GUILDS` block in `.env.example`, the Decat section of `docs/deployment.md`,
the retention rows in `docs/legal/data-requests.md`, the Alone Time retention
bullet *and* the whole "one feature is an AI" section of `PRIVACY.md`, the `/decat`
paragraph of `TERMS.md` §8, and the listing in `docs/discord-directory.md` — which
now reads 17 systems, 98 commands, and four switched off.

**`decat` and `alone` are in `utils/commands_sync.RETIRED_COMMANDS` now**, which is
what deletes the stale registrations from every server's picker. Without it they
stay registered and fail on click for whoever finds them, which is exactly how
`/dbstatus` and `/music` behaved before that list was checked against the archive
instead of against anybody's memory.

**Left in place on purpose:** the `alone_time` table and its helpers in
`utils/db.py`, because deleting them would drop stored rows for no benefit and a
restore wants the wood rather than the furniture. The Mac's own `~/.decat`
(config, switch state, and the `Public` history file) is the operator's to delete;
the login item and the switch are stopped, and the two Ollama models stay pulled.

---

Archived on 2026-10-06 (second pass), at the owner's request: **the starboard, Server
Structure, the company companion, radio, and Anonymous Q&A** — *"remove starboard,
company, legacy, and optimise the codebase and cmds"*, with `/shop` and `/buy`
deliberately kept (they do different work: browsing and buying) and `/meeting create`
changed in the same pass so that it **always** opens its private draft card first.

Four of the five were systems a server could switch on from the dashboard; **legacy**
was the module whose only command left was `/bot legacy` (the switch that turned Q&A
on), so the two retired together. Between them they held every system that had been
kept alive for a small audience rather than because the product needed it.

| Feature | File | Notes |
|---|---|---|
| Starboard | `cogs/starboard.py` | `/starboard set`, `/starboard status` and the reaction listener; `starboard_posts` / `starboard_settings` stay in the database. |
| Server Structure | `cogs/structure.py`, `utils/structure.py`, `utils/confirmations.py` | The confirmed channel/role management tier — `/structure *`. `utils/confirmations.py` was imported by this cog alone, so it travelled with it. |
| The company companion | `cogs/company.py`, `utils/company.py` | The one thing the bot did that nobody asked for: joining a voice channel whose only occupant was one person. |
| Radio | `cogs/radio.py`, `utils/radio.py`, `utils/radio_catalogue.py`, `utils/radio_stream.py`, `templates/radio.html`, `scripts/probe_stations.py` | `/radio play`, `/radio stop`, `/radio doctor` — archived with the station table and the player, and with the probe that rebuilt the table. |
| Anonymous Q&A, and the legacy switch | `cogs/qna.py`, `utils/qna.py` | `/qna ask`, `/qna inbox`, `/qna reply`, and `/bot legacy` — the switch that was their only remaining reader. |
| The suites | `tests/test_starboard.py`, `tests/test_company.py`, `tests/test_radio.py`, `tests/test_qna.py`, `tests/test_legacy_module.py`, `tests/test_guardrails.py` | Verbatim. `test_guardrails.py` held two things: the management tier's own ceilings, and the tree-wide law that every direct write is written down. The first half belongs to Server Structure and was archived with it; the second was **re-created as the live `tests/test_direct_writes.py`**, because it is about `cogs/` rather than about any one feature. |
| Starboard's half of a shared suite | `tests/test_starboard.py` (from `tests/test_starboard_and_admin.py`) | That file held starboard *and* the console's coin-removal laws; the live `tests/test_admin_coin_removal.py` is the second half, which is still about a page that exists. |

**What was removed from the live bot**: the four `Feature(...)` entries (plus
`legacy`) in `utils/features.py` — **17 systems → 13, 98 commands → 93** — every
`starboard`, `structure`, `company`, `radio`, `qna` and `bot legacy` row in
`utils/permissions.py`, the five `HELP_MODULES` categories and their home-body bullets
in `cogs/help.py`, `/bot legacy` in `cogs/bot_config.py`, the `PRO_MODULES` set in
`utils/plans.py`, the `structure` row in `utils/health.py`'s checks, the stream half of
`utils/media_health.py` (its third kind of target, `STREAM_HOSTS`, `stream_state()` and
the `radio_stream` import), the `QNA_WEBHOOK_URL` and `STREAM_PROBE_TARGET` settings in
`utils/config_audit.py` and their blocks in `.env.example`, in `keep_alive.py` the
`/radio` + `/music` routes and their page, `/api/voice-debug`'s stream probe, the
`questions` page and its two `/api/console/questions*` routes, the `company`/`radio`
keys in the live payload and the now-unused `url_for` import, in the console
`pages/Questions.tsx` (deleted), the Live page's Radio panel and Companion gap, the
`questions`/`radio`/`company` blocks of `api.ts`, `types.ts` and `lib/payload.ts`, in
the templates the dashboard's radio card and the `console_page == 'questions'` block,
the nav and footer links to `/radio` in all three site templates, `templates/radio.html`
itself, the `.radio-row` block in `static/pages.css`, the radio and Q&A laws of
`tests/test_site.py`, `tests/test_pages_site.py`, `tests/test_theme.py`,
`tests/test_meoww_site.py`, `tests/test_ai_guard.py` and `tests/test_media_health.py`,
`scripts/build_site.py`'s `/radio` page and its 301, `scripts/preview_dashboard.py`'s
radio/company/questions fixtures, and the Q&A and starboard paragraphs of
`PRIVACY.md`, `TERMS.md`, `docs/legal/data-requests.md`, `docs/discord-directory.md`
and `docs/architecture.md`.

**`starboard`, `structure`, `company`, `radio` and `qna` are in
`utils/commands_sync.RETIRED_COMMANDS` now**, which is what deletes their stale
registrations from every server's picker on the next publish. That list only deletes
what the registry no longer declares, so it is safe to leave in place — and it is the
reason these five names will disappear from Discord without anybody deleting them by
hand.

**Left in place on purpose:** the `starboard_posts`, `starboard_settings`, `radio_stay`,
`radio_last` and `qna_questions` tables in `utils/db.py`, including the question rows —
an unanswered question is somebody waiting for an answer, and a deletion request still
reaches them (`docs/legal/data-requests.md` says so in the same words). The
`guild_modules` rows that switched these systems on are left alone for the same reason
the voice-chat watch's were: harmless, and the state a restore wants back.

**Not an archive, but part of the same pass:** `/meeting create` now opens its private
card every time, whatever it was given on the command line — the options *seed* the
draft and nothing is written until **Book it** is pressed. The one-shot path was deleted
rather than kept beside it, so there is one set of promises (`Meetings._book`) instead of
two that could drift; `/shop` and `/buy` were left exactly as they were.

**And one module that nothing imported at all:** `utils/update_notify.py` (137 lines)
had no caller anywhere in the tree — the release-note path it was written for lives in
`bot.py` and `utils/version.py` now — so it went, along with the two settings only it
read (`UPDATE_CHANNEL_ID`, `UPDATE_ROLE_ID`, from `utils/config_audit.py` and
`.env.example`). Both directions of the audit law are what proved it: the registry
cannot describe a setting nothing reads, and `.env.example` cannot document one.

## How to restore a feature

**Steps 1–5 name the places these things lived in 2026-09-13; the registry is now the
only source of them** — `utils/features.py` holds the `Feature(...)` entries, and
`cogs/help.py`, `utils/permissions.py`, the dashboard's switches, the site and the
command scope all derive from it. A restored feature wants its `Feature(...)`, its
`HELP_MODULES` category and its access rows, and then `cogs.<name>` back in the
registry's own `cog` field rather than in `bot.py`.

1. Move the file back: `mv deleted_features/cogs/<name>.py cogs/`
2. Re-add `cogs.<name>` to `INITIAL_COGS` in `bot.py` (see the note above: the
   registry's `cog` field is what loads it now).
3. Re-add its command names to `MODULE_COMMAND_MAP` in `bot.py` (now
   `features.MODULE_COMMAND_MAP`, derived from `gate_commands` and `SHARED_GATES`).
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
8. For **the voice-chat watch** only: move `vc_watch.py` back to `utils/`, move
   both suites back to `tests/`, re-add `Feature(id="watch", ...)` to
   `utils/features.py`, `"watch": ADMIN` to `utils/permissions.py`, the two
   `Setting(...)` entries to `utils/config_audit.py` and both names to
   `.env.example`, restore the `seed_home` call in `bot.py` (its import line
   reads `from utils import bot_channels, vc_watch`), restore the listener and
   `/watch` in `cogs/moderation.py` (they sat between the cog's
   `interaction_check` and its `# ── helpers ──` section, and the cog no longer
   imports `time`), the `context["watch"]` block on the Live page and the
   `vc_watch` import in `keep_alive.py`, the `{% if watch %}` card in
   `templates/owner.html`, and `HELP_MODULES["watch"]` in `cogs/help.py`.
9. For **Decat and Alone Time** (the 2026-10-06 archive): move
   `deleted_features/cogs/decat.py` back into `cogs/`, `deleted_features/decat/`
   back to the repository root, and `deleted_features/cogs/alonetime.py` and
   `deleted_features/utils/alonetime.py` back into `cogs/` and `utils/`; move the
   three suites out of `deleted_features/tests/`; re-add both `Feature(...)`
   entries to `utils/features.py` and every `alone*`, `decat` and `bot decat` row
   to `utils/permissions.py`; restore the `HELP_MODULES` categories and home-body
   bullets in `cogs/help.py`, `/bot decat` in `cogs/bot_config.py`, the two
   `alonetime.lock_for` guards in `cogs/company.py`, the three `/api/decat/*`
   routes and the `/api/alone-time` door with `_alone_time_reading` in
   `keep_alive.py`, the panel in `templates/dashboard.html` and the fixture in
   `scripts/preview_dashboard.py`, both blocks in `.env.example`, and the removed
   paragraphs of `PRIVACY.md`, `TERMS.md`, `docs/deployment.md`,
   `docs/legal/data-requests.md` and `docs/discord-directory.md` (17 systems and
   98 commands become 19 and 100 again). **Then drop `decat` and `alone` from
   `commands_sync.RETIRED_COMMANDS`** — that list is what deletes them from
   Discord's picker, and it would delete them again the moment they were
   republished. On the laptop, `bash decat/mac/install.sh` installs it all again
   (the models are still pulled; the login item was unloaded, not removed).
10. For **uploads** only, also drop `upload`, `upload-big` and
   `pin-upload-info` from `commands_sync.RETIRED_COMMANDS` in
   `utils/commands_sync.py` — that list is what deleted them from Discord's
   command picker, and it would delete them again the moment they are republished.
11. For **the five retired on 2026-10-06** (second pass: starboard, Server
   Structure, the company companion, radio, Anonymous Q&A and `/bot legacy`): move
   the archived cogs and utils back from `deleted_features/cogs/` and
   `deleted_features/utils/`, `templates/radio.html` back into `templates/` and
   `scripts/probe_stations.py` back into `scripts/`; move the archived suites out of
   `deleted_features/tests/` (note that `test_guardrails.py` and
   `test_starboard_and_admin.py` were split, so what is live under those names today is
   deliberately not the same file); re-add the five `Feature(...)` entries to
   `utils/features.py` and every row to `utils/permissions.py`; restore the
   `HELP_MODULES` categories in `cogs/help.py`, `/bot legacy` in `cogs/bot_config.py`,
   the `PRO_MODULES` entry in `utils/plans.py`, the `structure` row in
   `utils/health.py`, the stream half of `utils/media_health.py`, the two settings in
   `utils/config_audit.py` and their blocks in `.env.example`; in `keep_alive.py`
   restore the `/radio` page and routes, the `/api/console/questions*` routes and the
   `OWNER_PAGES` entry, the `voice-debug` stream probe and the `company`/`radio` keys of
   the live payload; in the console restore `pages/Questions.tsx`, the Live page's Radio
   panel and the three blocks of `api.ts` / `types.ts` / `lib/payload.ts` (then
   `cd console && npm run build`, which stamps the bundle the suite compares against);
   restore the dashboard's radio card and the questions block in
   `templates/dashboard.html`, the nav and footer links in the three site templates, the
   `/radio` page in `scripts/build_site.py` and its 301, and the paragraphs this pass
   rewrote in `PRIVACY.md`, `TERMS.md`, `docs/legal/data-requests.md`,
   `docs/discord-directory.md` and `docs/architecture.md`. **Then drop `starboard`,
   `structure`, `company`, `radio` and `qna` from `commands_sync.RETIRED_COMMANDS`** —
   while they are there, the publisher deletes them again the moment they are
   republished. `utils/update_notify.py` is not part of this: nothing imported it, and
   restoring it means finding a caller first.
