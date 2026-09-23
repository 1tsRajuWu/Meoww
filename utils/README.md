# Utils 🧰

Shared helpers behind the cogs. Rule of thumb: **pure logic lives here**
(plain inputs → plain outputs, no `discord` import where avoidable) so tests
can call it directly without mocking Discord.

| File | What it's for (plain words) |
|---|---|
| `features.py` | **The feature registry.** Every feature declared once (id, name, commands, cog). Dashboard cards, command gates, help menu all derive from it. Edit this to add/retire a feature. |
| `modules.py` | On/off toggle state per server — the single cache, reader, and writer shared by bot + dashboard. |
| `permissions.py` | Who may run what (`COMMAND_ACCESS` table) + stamping picker visibility before commands publish. |
| `db.py` | SQLite schema, migrations, and `run_atomic()` for race-safe transactions. |
| `levels.py` | XP/level math and the max-level ceiling. |
| `rewards.py` | Message-effort + voice-presence payout formulas (pure functions). |
| `shop.py` | Shop catalog: 20 perks across 5 price tiers + effect rules. |
| `loans.py` | Virtual loan math. |
| `meowfee.py` | The 2% transfer-fee math (rate, floor, cap). |
| `market.py` | Live-price providers, instruments catalog, trade settlement, candles. |
| `commands_sync.py` | Compare-then-upsert command sync (never bulk-overwrite) + retired-command list. |
| `command_scope.py` | Which commands one server's switches leave on (pure rule). Hidden = declared by the registry **and** off here. |
| `command_publish.py` | What Discord came back with the last time each scope published: was it read, what is still to write, when, and any refusal in Discord's words — plus the one sentence the dashboard's scope panel prints (in memory; a publish belongs to this process's conversation with Discord). |
| `figures.py` | What this deployment is doing, as counts a **stranger** may read (servers, members, and the tracker's messages/voice/people/channels per window). Counts and never names; a reading that was never taken is absent rather than zero; cached a minute. Feeds the landing page's cards and the console's *What the bot is doing* panel through `/api/figures`. |
| `cloud_sync.py` | Supabase backup: restore on boot, upload loop, leases, shrink guards. |
| `style.py` | Card design system (`send_card`, `card`) — replies that never ping by accident. |
| `radio_catalogue.py` | **The station table**, as data: every row a URL that was fetched before it was written down, plus the closed tag vocabulary, the indexes and the autocomplete search. Nothing here touches Discord or the network. |
| `radio.py` | The radio's *policy*: the defaults (English, shuffle, ad-free, a held seat), the pool a request narrows, and the pick — including the two rules worth knowing, that a country or language is a **request** (never widened) while ad-free is a **preference** (never empties the room). |
| `radio_stream.py` | The engine: one station, one socket, the cut-stream guard, the bounded retry budget a healthy stretch restores, the 24/7 seat, and the probe `media_health` and `/radio doctor` share. |
| `voice_caps.py` | Whether this host can play anything at all — ffmpeg, libopus, `davey` — and *which* piece is missing, asked once at boot and read by `/radio doctor`, the consoles and `/api/voice-debug`. |
| `media_health.py` | The weekly check over every hardcoded media URL **and** one real station opened all the way to a byte, so a host that lost its route out reports itself. Covers the table automatically: `utils/radio_catalogue.py` is in its declared source paths. |
| `chart.py` | Candlestick/line chart image rendering (Pillow). |
| `card.py` | Rank/level card image rendering. |
| `admin_limits.py` | Caps on admin-settable numbers so a typo can't break the economy. |
| `update_notify.py` | Update-announcement helpers (what changed since last deploy). |
| `version.py` | Changelog hashing + digest for the "Bot Updated" post. |

If you're new: read `features.py` first, then whatever helper your feature
uses (`meowfee.py` for fees, `rewards.py` for payouts, `market.py` for prices).
