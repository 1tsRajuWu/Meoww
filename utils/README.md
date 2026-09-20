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
| `chart.py` | Candlestick/line chart image rendering (Pillow). |
| `card.py` | Rank/level card image rendering (archived music art lives here too). |
| `spotify.py` | Spotify link resolver (kept for the archived music feature). |
| `admin_limits.py` | Caps on admin-settable numbers so a typo can't break the economy. |
| `update_notify.py` | Update-announcement helpers (what changed since last deploy). |
| `version.py` | Changelog hashing + digest for the "Bot Updated" post. |

If you're new: read `features.py` first, then whatever helper your feature
uses (`meowfee.py` for fees, `rewards.py` for payouts, `market.py` for prices).
