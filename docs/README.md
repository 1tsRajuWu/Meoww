# Docs 📚

Start at the [main README](../README.md), then come back here when you need depth.

| Guide | When to read it |
|---|---|
| [`architecture.md`](architecture.md) | "How does a slash command actually work?" — the code tour with a diagram |
| [`deployment.md`](deployment.md) | Hosting the bot 24/7 (Render + Supabase), env vars, backups, monitoring |
| [`voice-debug.md`](voice-debug.md) | Voice XP/coins not paying? Diagnose it step by step |
| [`handoff.md`](handoff.md) | Maintainer handoff notes (history of the big audit) |

Live legal docs ([`PRIVACY.md`](../PRIVACY.md), [`TERMS.md`](../TERMS.md)) and the
[`CHANGELOG.md`](../CHANGELOG.md) stay at the repo root, because the bot reads
them at runtime (`/legal privacy`, `/legal tos`, update announcements). The
operator-facing half — the retention schedule, the deletion procedure and the
third-party list — lives in [`legal/`](legal/README.md), and
[`tests/test_legal.py`](../tests/test_legal.py) fails when either half stops
matching the code.
