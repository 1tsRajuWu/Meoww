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
| `voice_caps.py` | Whether this host can play anything at all — ffmpeg, libopus, `davey` — and *which* piece is missing, asked once at boot and read by `/api/voice-debug`. |
| `media_health.py` | The weekly check over every hardcoded media URL and every action/pet picture the gifs tables resolve, so a dead link is news rather than a blank box in a card nobody looked at. Two kinds of target since the radio was archived (2026-10-06); a third — a real station opened to a byte — went with it. |
| `chart.py` | Candlestick/line chart image rendering (Pillow). |
| `card.py` | Rank/level card image rendering. |
| `admin_limits.py` | Caps on admin-settable numbers so a typo can't break the economy. |
| `version.py` | Changelog hashing + digest for the "Bot Updated" post. |

If you're new: read `features.py` first, then whatever helper your feature
uses (`meowfee.py` for fees, `rewards.py` for payouts, `market.py` for prices).
