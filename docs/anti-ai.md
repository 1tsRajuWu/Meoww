# Anti-AI collection: the posture, and what to do when it fires

The policy is `AI_POLICY.md`; the Terms' clause is section 14. This document is
the operator's half: where each piece lives, what a card in Discord means, and
what to do about it in the fifteen minutes after it arrives.

The reason all of this exists is one sentence in the framework it was built from:
**`robots.txt` is a request, and the collectors with the most to gain are the
ones least likely to honour it.** So there are three layers, and each one exists
because the one before it can be ignored.

| Layer | Where | What it does |
|---|---|---|
| The directive | `utils/ai_guard.robots_txt()` / `ai_txt()` → `/robots.txt`, `/ai.txt`, `/.well-known/ai.txt` and the same two files in every static build | Names each AI crawler, disallows `/` for it, and states the training prohibition in the convention (`Disallow` inside `ai.txt`). |
| The refusal | `ai_guard.crawler()` + the `before_request` hook in `keep_alive.py` | A declared AI crawler gets **403** with the addresses of the two documents it is ignoring, on every path except the directives themselves. |
| The trap | `ai_guard.HONEYTOKENS` + `templates/_trap.html` | Paths no person can reach, disallowed in the directive, absent from the sitemap, off-screen and `tabindex="-1"` in every public footer. A fetch is recorded with agent, address, country and count. |

The reservation also travels as headers on **every** response —
`Content-Usage: train-ai=n`, `tdm-reservation: 1`, `tdm-policy`, and
`X-Robots-Tag: noai, noimageai` (plus `noindex, nofollow` on the surfaces behind
a sign-in). A header reaches an API answer, an image and an error page as readily
as it reaches HTML, which is why it is not left to the file.

## When the card arrives

`operator_log.ai_guard_alert` posts one card to the operator's own server: a trap
fetch (which no legitimate agent can produce) or a named crawler that has been
refused `FLOOD_THRESHOLD` times inside the fold window. It carries the agent, the
address and country, the path, the count, and what actually stops the thing.

1. **Block the network, not the string.** A user-agent rule costs the collector
   one line to defeat; an address range costs it the proxies. In the Cloudflare
   dashboard for the zone that fronts the public site, this is a WAF custom rule:

   ```
   (http.user_agent contains "GPTBot") or
   (http.user_agent contains "ClaudeBot") or
   (http.user_agent contains "CCBot") or
   (http.user_agent contains "Bytespider") or
   (http.user_agent contains "PerplexityBot")
   → Block
   ```

   and for a repeat offender from one network, an IP Access rule on the range the
   card named (or a Country rule if the whole country is the source — cheap, and
   it blocks real visitors too, which is the tradeoff to make deliberately).
2. **Keep the evidence.** The row is already in `net_guard_events`, retained for
   180 days, and `/health` carries the counts. Do not clear it; it *is* the
   record, and it is the one that answers "what did it take and when".
3. **Only then decide about letters.** For a commercial collector, a fetch of a
   disallowed path that no page links a person to — from a host that had already
   been told, in three places — is the fact a complaint is built on. Section 14
   of the Terms makes it a breach; where the collector passed a sign-in gate it
   did not have permission to pass, it is unauthorized access as well.

## A false positive is a real cost

The guard refuses only agents whose published token is in
`utils/ai_guard.AI_CRAWLERS`, so the plausible false positives are: a headless
browser used by a monitor, a `curl` script that names itself like a crawler, and
a site-search integration. When one is reported:

* **Say which token matched**, and remove it from the list if the agent is
  legitimate — that is a one-line change plus a test in `tests/test_ai_guard.py`.
* **`Googlebot`, `bingbot` and `PetalBot` are deliberately not in the list.**
  Refusing search would take the public pages out of the one index that brings
  people to them, and the reservation is about training rather than about being
  found. Adding a general search crawler is a decision to be made out loud.

## Maintaining it

* **A new AI crawler**: add `(token, label)` to `AI_CRAWLERS`. It appears in
  `robots.txt` on all four hosts, in the refusal table and in the tests at once,
  because all four read the same tuple.
* **A new trap path**: add it to `HONEYTOKENS`. The directive and the route are
  generated from that tuple; the *markup* is the one hand-written piece
  (`templates/_trap.html`), and `tests/test_ai_guard.py` fails if its `href`
  stops matching the first entry.
* **A new host**: call `ai_guard.robots_txt()` and `ai_guard.ai_txt()` from its
  build, and add `ai_guard.TRAIN_HEADERS` to whatever writes its headers file.
  `tests/test_ai_guard.py` fails on a build script that writes
  `User-agent: * / Allow: /` by hand, which is how three of the four hosts ended
  up with the honest answer on one and nothing on the next.

## Checking it locally

```bash
# What the app itself serves
curl -s localhost:5000/robots.txt | head -20
curl -s localhost:5000/ai.txt | head -12
curl -sI localhost:5000/ | grep -i -E 'robots-tag|content-usage|tdm'

# The refusal, and the trap
curl -s -o /dev/null -w '%{http_code}\n' -A 'GPTBot/1.2' localhost:5000/
curl -s localhost:5000/api/_index/entities.json | head -6
```

The last one writes a row into the ledger and prints it on `/health` as
`anti_ai.traps` — which is also the fastest way to see the whole chain work
before a real collector does.
