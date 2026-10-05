# Cogs 🧩

One file = one feature = one Discord "cog" (a bundle of slash commands).
Newbie rule: **if you want to change what a command does, the file below is
where it lives.** Shared logic (money, XP, prices) lives in `../utils/` so
cogs stay thin.

| File | Feature | Commands you'll recognize |
|---|---|---|
| `alonetime.py` | Alone Time | `/alone start stop status guests ask list release server`, the voice sweep, and the holder's whitelist/rename/release controls |
| `automod.py` | Automod & Shield | `/automod invites`, `/automod antispam` |
| `bot_config.py` | The bot itself | `/bot db`, `/bot push`, `/bot sync`, `/bot avatar`, `/bot banner`, channel bindings |
| `decat.py` | Decat | `/decat` — a question relayed to the operator's own Mac, answered there with live sources. Thin on purpose: the queue, the words and the machine half all live in `../decat/` |
| `economy.py` | Economy & Casino | `/balance /daily /work /rob /pay /shop /buy /loan …` |
| `fun.py` | Fun & Memes | `/meme /8ball /meow /quote /roast …` |
| `games.py` | Mini-Games Hub | `/tictactoe /rps /trivia /mines /crash …` (plus casino games shared with economy) |
| `help.py` | Help menu | `/help` (categories mirror `utils/features.py`) |
| `legal.py` | Legal docs | `/legal privacy`, `/legal tos` (rendered from root `PRIVACY.md` / `TERMS.md`) |
| `leveling.py` | Leveling & XP | `/rank /leaderboard /levelroles view|add|remove /givexp /setlevel …` + message/voice listeners |
| `market.py` | Market | `/market board quote chart buy sell portfolio history` |
| `meetings.py` | Meetings & Notices | `/meeting create list view reschedule ping cancel` + RSVP buttons |
| `moderation.py` | Moderation & Logs | `/ban /kick /mute /warn /purge /modlog …` |
| `server_control.py` | Server utilities | `/serverinfo /userinfo /announce /lockdown /slowmode /defaultrole …` |
| `social.py` | Social Roleplay | `/action /marry /ship /hunt /zoo /uwu …` |
| `starboard.py` | Starboard | `/starboard set limit status` + reaction listener |
| `tickets.py` | Support Tickets | `/ticket setup` + panel buttons + ratings |
| `tracker.py` | Activity Tracker | `/activity /activity-top /activity-channel` |

Every gated command starts with:

```python
if not self.bot.is_module_enabled(interaction.guild_id, "<feature-id>"):
    return
```

…where `<feature-id>` is declared in `../utils/features.py`. The test suite
fails if a command forgets this, so you'll know immediately.
