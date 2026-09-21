# Legal documents

The three documents a person is bound by live at the repository root, because
the Bot reads them at runtime, `/legal` renders two of them inside Discord and
the dashboard asks for all three before the console opens:

| Document | Served by | What it covers |
|---|---|---|
| [`PRIVACY.md`](../../PRIVACY.md) | `/legal privacy`, `/privacy` | what is stored, what is not, where it lives, who else sees it, how long it is kept, the lawful bases, your rights under the GDPR / UK GDPR / CCPA-CPRA / DPDP Act, cookies, transfers, how to have it removed |
| [`TERMS.md`](../../TERMS.md) | `/legal tos`, `/terms` | what the Bot is, staff responsibilities, the virtual economy and market, acceptable use, availability, liability, output ownership, the anti-AI clause, automated filters, no professional reliance, third parties, indemnity, arbitration with a class-action waiver, notices and the grievance officer, export controls, changes |
| [`AI_POLICY.md`](../../AI_POLICY.md) | `/ai`, and machine-readable at `/ai.txt` | what training use is reserved against, what is welcome, how the reservation is stated technically (directives, headers, refusals, traps), what happens when it is ignored, and the two facts about AI *in this project* |

The third one is not an agreement — it is a statement about the deployment, which
is why it is readable without signing in and why the same text is at `/ai.txt` for
a program. The reservation itself is a clause in the Terms (section 14), because a
convention a crawler may ignore is not a term; the enforcement is
[`docs/anti-ai.md`](../anti-ai.md).

## Agreeing, and what records it

The dashboard is a **clickwrap**: the three documents are shown at the door and an
affirmative press is what opens it (`utils/consent.py`, `/agree`). What is
recorded is the **revision** — the SHA-256 prefix of the three files themselves,
not a hand-kept number — with the scope, the address, the browser and the time. A
sentence added to a document is a new revision, so an agreement made before it
does not cover it, and the dashboard asks again at the next sign-in. Two rules are
pinned by tests rather than trusted: **the two answers are the same size** (same
class, same width, no pre-selection, no countdown, no confirmshaming) and **a
record that cannot be written says so** instead of letting the person in silently.

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
  deleted);
* the anti-AI clause, the arbitration section, the indemnity, the grievance
  officer or the liability cap disappears from the Terms;
* the clickwrap stops covering the AI policy, or its revision stops changing when
  a document does (`tests/test_legal.py::TestAIDisclosure` and
  `tests/test_consent.py`);
* a Discord card for a document grows past the embed ceiling *including* the
  field that says where the rest of it is — the failure this repository already
  had once, when the Terms outgrew the 6,000-character budget and the pointer was
  added after the fill.

When you change the behaviour these documents describe, change the documents in
the same commit. Run:

```bash
python -m pytest tests/test_legal.py -q
```
