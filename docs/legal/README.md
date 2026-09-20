# Legal documents

The two documents a member is bound by live at the repository root, because the
Bot reads them at runtime and `/legal` renders them inside Discord:

| Document | Served by | What it covers |
|---|---|---|
| [`PRIVACY.md`](../../PRIVACY.md) | `/legal privacy` | what is stored, what is not, where it lives, who else sees it, how long it is kept, how to have it removed |
| [`TERMS.md`](../../TERMS.md) | `/legal tos` | what the Bot is, staff responsibilities, the virtual economy and market, acceptable use, availability, liability, subscriptions, changes |

These are the operator-facing documents behind them:

| Document | Audience |
|---|---|
| [`data-requests.md`](data-requests.md) | the operator: how to answer an access or deletion request, store by store, without the next backup putting the data back |
| [`sub-processors.md`](sub-processors.md) | the operator: every external service the Bot talks to, what each one receives, and how to drop one |

[`SECURITY.md`](../../SECURITY.md) is the fourth: how to report a vulnerability,
and what the deployment does about secrets.

## These documents are contracts with the code

Every factual claim here has to be checkable, because a policy that describes a
different bot than the one running is worse than no policy. So the claims that
can drift are pinned by `tests/test_legal.py`, which renders the real files
through the real renderer and fails when:

* a section stops arriving on the Discord card, or the card exceeds Discord's
  limits (a policy a member cannot read is not a policy);
* the retention windows in the text stop matching the constants in the code
  (`cogs/tracker.py::RETENTION_DAYS`, `utils/scheduled.py::HISTORY_DAYS`,
  `utils/cloud_sync.py::BACKUP_KEEP` and `SNAPSHOT_INTERVAL`);
* a third party stops being named, or one is named that no code calls;
* the session cookie or sign-in link the policy describes stops matching what
  `keep_alive.py` configures;
* the AI disclosure stops acknowledging creation with AI coding assistants and
  autonomous AI agents, or a runtime AI provider is introduced without updating
  the disclosure;
* `.env.example` documents a variable no code reads (which is how a backup
  encryption key and a Gist backup were documented for a code path that had been
  deleted).

When you change the behaviour these documents describe, change the documents in
the same commit. Run:

```bash
python -m pytest tests/test_legal.py -q
```
