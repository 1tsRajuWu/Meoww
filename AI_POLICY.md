# AI & Crawling Policy

**Effective Date:** October 1, 2026 · **Version 6**.

This is the third document in the bundle a dashboard sign-in agrees to, and the
shortest way to say what it says is: **people are welcome here, collectors are
not.** It covers the Bot, the public site, the hub and the owner console —
everything this project publishes — and it is the same policy wherever it is
written: here for a reader, `robots.txt` and `ai.txt` for a program, and the
response headers on every page for whichever of the two looks there first.

## 1. What is reserved

**Training use of anything here is reserved against.** No licence — express,
implied or by estoppel — is granted to use this project's text, its pages, its
rendered cards, its command output, its screenshots or the contents of any
Discord server reached through the Bot for:

* training, fine-tuning, distilling, aligning or evaluating a machine-learning
  model, including an embedding model;
* building, extending or cleaning a dataset for any of the above;
* benchmarking or comparing such a model on this material in a published or
  commercial evaluation; or
* feeding any of it to a service that does any of the above on your behalf.

This is the same rule as section 14 of the Terms of Service, in the place a
person reads for it. Where the two ever appear to differ, the Terms govern the
parties to the Terms and this document governs a reader who is not a party.

## 2. What is welcome

* **People reading.** Browsing, reading, quoting, linking, criticising,
  summarising for somebody else, and teaching from it, with attribution.
* **Search engines** indexing the public pages so somebody can find them. The
  reservation is about training, not about being found; Googlebot and bingbot are
  not refused, and the sitemap is published for them.
* **Archives and research** with written permission, which is free to ask for and
  usually given: say what you want, how much of it, and what it is for, in the
  support server.
* **A person using an AI assistant** to read, translate or summarise a page for
  themselves, the way they would use a screen reader or a translator. That is a
  reader with a tool; what is prohibited is collection at scale for a corpus.

## 3. How it is stated, technically

* **`robots.txt`** on every host, listing each known AI-training and
  data-broker crawler by name with `Disallow: /`, then a wildcard section that
  allows the public pages and disallows the signed-in surfaces and the trap paths.
  Named crawlers are listed individually so that ignoring the file is a choice
  with an author and not an oversight.
* **`ai.txt`** — the training prohibition in the form crawlers look for, with the
  reservation spelled both ways (`tdm-reservation: 1`,
  `content-usage: train-ai=n`).
* **Response headers on every page** — `X-Robots-Tag: noai, noimageai` (or
  `noindex, nofollow, noai, noimageai` behind a sign-in), `Content-Usage:
  train-ai=n`, `tdm-reservation: 1` and `tdm-policy`. A header reaches an answer,
  an image and an error page as readily as it reaches HTML.
* **Refusal.** A crawler that names itself as one of the listed AI agents is
  answered `403` and told which documents it is ignoring, on every path except
  the two directive files above.
* **Traps.** Two paths exist that no page links a person to, that `robots.txt`
  disallows for everybody, and that answer with a small document that says what
  it is: *this path is not part of the site; it exists to identify automated
  collection that ignores `robots.txt`.* Nothing legitimate fetches one — they
  are `tabindex="-1"` and `aria-hidden`, absent from the sitemap, and reached
  only by reading markup and following every link in it.

## 4. What happens when it is ignored

A trap fetch is **recorded**, not guessed at: which path, which agent, the
address and the country the request came from, and how many times inside a short
window, kept for **180 days**. Above a small threshold the operator is told in
Discord, in the same channel as the other operational alerts, with the two facts
that lead to a decision.

The operator then does what the situation calls for, in this order: it blocks the
**network** rather than the string, because a user-agent rule costs one line to
defeat and an address range costs the collector something; it keeps the evidence;
and where a commercial entity has collected this material, it treats the
collection as a breach of the Terms and, where the collector passed through a
sign-in or a sign-in gate that it did not have permission to pass, as
unauthorized access to a computer system as well.

**A false positive is a real cost, and it is fixable.** If your browser, your
monitor, your integration or your own test suite was refused — a headless
browser string, a shared address range, a curl script with an agent that reads
like a crawler — say so in the support server. The token or the range will be
removed, and this policy will say which one it was if it keeps happening.

## 5. The AI this project actually uses

Two facts, and both matter:

* **The running Bot is not AI.** No model is called, no inference service is
  contacted, and nothing anybody sends reaches one. That is a claim about the
  code, and the repository's own test suite fails if a provider ever appears in
  it, so it cannot quietly stop being true.
* **The source was written with AI assistance** — AI coding assistants and
  autonomous agents, working from human direction, reviewed by a human before it
  shipped. Saying so is part of the honesty this document asks of everybody else:
  a project that reserves its text against training should be clear about the
  tools that built it.

## 6. Asking, complaining, requesting

Everything in this document is handled through the bot's own support server:

* **Permission** to use this material for something this policy reserves —
  describe the use, the volume and the purpose.
* **A false positive** — say what was refused and what you were doing.
* **A correction** to a record this project keeps about your collection, or a
  deletion request for it, which is answered the same way the Privacy Policy
  describes for personal data.
* **A complaint** about the Bot as an intermediary, or about personal data — the
  grievance officer named in the Terms, acknowledged within 7 days and resolved
  within 30.

Nothing in this policy limits anybody's rights under the law that applies to
them, and nothing in it prohibits a use the law expressly permits — including
text and data mining for scientific research by a body entitled to do it, and
the exercise of any statutory right to quote or link.
