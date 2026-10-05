# Terms of Service

**Effective Date:** October 6, 2026 · **Version 8** (AI-assisted source).

By inviting, configuring or using this Discord bot ("the Bot"), or by signing in
to its dashboard and pressing the agreement there, you agree to these Terms. If
you do not agree, remove the Bot from your server and do not use the dashboard.
They are between you and the operator of the deployment you are using.

## 1. What the Bot is

The Bot is a self-hosted community bot: moderation, logging and automod filters;
tickets with transcripts and close-time ratings; a leveling system with XP and
automatic role rewards; a coin economy with a shop, loans and casino games; a
live market; a starboard; social roleplay; fun commands and mini-games; a
per-channel activity tracker keeping a 365-day window; and a web dashboard for
staff — module toggles, a message composer that can post **now or on a schedule**
in up to five channels, live market charts, and a bug-report command. It is
provided **"as is" and "as available"**, with no warranty of any kind; it may
change, break or end at any time.

## 2. Your responsibilities as staff

* You decide permissions. The Bot enforces what Discord and the operator
  configured; it cannot judge whether staff should hold those powers. Configure
  roles deliberately.
* Commands changing data — moderation, role assignment, economy admin, level
  roles, module toggles, announcements, the market's role — act on your
  instruction, as does dashboard activity. The composer speaks **as the Bot**:
  what it posts is your message, and scheduled posts go out automatically. The
  operator is not responsible for what staff or members send with it.
* Keep the dashboard passkey and sign-in links private. Access is the
  credential; actions taken with it are treated as your instruction. Sign-in
  links are single-use and expire in 15 minutes. Where Discord OAuth is enabled,
  your Discord account is the credential: only servers where you are
  Administrator, or the server's owner, are accessible, so removing roles in
  Discord revokes access.
* Scheduled messages depend on host uptime. Posts delayed over 24 hours are
  skipped with notice, and repeats halt after three consecutive failures.
* The Bot does not replace human moderation, and automated filters can be
  imperfect. Review what it does.

## 3. Virtual currency, items and the market

* **Coins, items, loans and market positions are virtual.** They carry no cash
  value, cannot be redeemed for real money, and confer no claims against the
  operator. Nothing in the Bot is financial advice.
* **The market quotes real prices** from public exchange endpoints (see the
  Privacy Policy). Trades settle 1:1 in virtual coins: real price movements are
  mirrored, real funds are not.
* **Quotes may be delayed, erroneous or unavailable.** If endpoints fail, trading
  pauses rather than executing against stale quotes.
* **Limits apply and may change:** minimum and maximum order sizes, rolling daily
  payout caps per member, and trade cooldowns. Access is role-gated.
* Prices originate from third parties outside operator control; the operator is
  not liable for market quote inaccuracies or virtual coin losses.
* Loans represent virtual debt with in-Bot penalties only (interest, borrowing
  holds); they never constitute real-world debt.

## 4. Acceptable use

Do not use the Bot to violate Discord's Terms of Service, Community Guidelines,
or applicable law. Do not exploit bugs, race conditions, or scripted automation
to acquire coins, XP, items, roles, or unauthorized privileges; report flaws
promptly. Filing a bug report you know to be false, or spamming the report and
feedback tools, is itself misuse. Do not spam, bypass cooldowns, multi-account
farm, or attack the Bot, its host, or upstream data sources. Do not collect from
the Bot or its website by automated means, and do not use either to train a
model — section 14 is the whole of that rule. The operator may reset balances,
revoke access, or ban users and servers for abuse. Loopholes closed after the
fact may void associated gains.

## 5. Availability, backups and data loss

The Bot runs on a cloud host and may experience downtime during maintenance or
provider limits. Data is stored in SQLite, restores from a private Supabase
backup at launch, and synchronizes periodically with hourly snapshots kept for
recovery — but **no backup is guaranteed**. Stored data prunes automatically per
retention windows (365 days of activity, 90 days of dashboard message history).
The database file is operator-accessible; do not store confidential information.
See the Privacy Policy for deletion requests.

## 6. Limitation of liability

To the maximum extent permitted by law, the operator is not liable for direct,
indirect, incidental, or consequential damages resulting from Bot usage,
including data loss, lost virtual items, server disruption, or moderation
decisions. Where liability cannot be fully disclaimed, it is limited to fees
paid by you in the preceding twelve months, or zero if using free services.

**The cap is the whole of it.** No claim may be brought more than twelve months
after the event giving rise to it. Nothing here limits rights guaranteed by
mandatory law, and nothing here disclaims liability for the operator's fraud,
wilful misconduct, or anything else that cannot lawfully be disclaimed where you
live.

## 7. Age

The Bot follows Discord's minimum age requirement. Do not use the Bot or
submit personal data if you do not meet Discord's age criteria. The Bot is not
directed at children, and the operator does not knowingly hold a child's data.

## 8. AI assistance

**The Bot itself calls no AI model**, and nothing you send it reaches one.
Created and maintained **with the help of AI coding assistants and autonomous
AI agents**, and that assistance is disclosed in the source repository's history
rather than only here. One optional feature, `/decat`, does *answer with* a model
— one running on the operator's own computer, never on a vendor's service, and
the question is relayed to nothing else. It is switched off in every server until
an administrator turns it on, and the Privacy Policy states exactly what it keeps
and what it does not. The third document in this bundle,
the AI & crawling policy, states what this deployment does about automated
collection and about training use.

## 9. Subscriptions and operational fees

Core Bot functionality is currently offered without charge. To support ongoing
hosting, database maintenance, infrastructure scaling, and operational costs,
optional paid subscription tiers, server premium perks, or enhanced quotas may
be introduced. If they are, each one will state its price, its billing period,
whether it renews automatically, how to cancel, and what a cancellation or a
refund does to already-earned progress **before** it is purchased — and no paid
tier will bundle consent to anything else, including model training, with the
thing you are buying.

## 10. What you send, and what the Bot produces

* **What you send stays yours.** As between you and the operator, you keep every
  right you already had in the text, settings, images and commands you provide.
* **What the Bot produces is yours to use**, and to the extent the operator holds
  any right in it, the operator assigns that right to you. That covers composed
  messages, ticket transcripts, cards the Bot renders, and market charts.
* **Output is not unique, and that is not an infringement.** The Bot renders from
  templates, statistics and third-party data, so two servers can receive
  identical or near-identical text, and a card can resemble one another server
  already has. Receiving the same thing as somebody else is not a claim by
  either of you against the other, and the operator makes no promise of
  exclusivity.
* **You warrant that you may send what you send.** You must hold the rights and
  permissions needed for anything you give the Bot — a ticket description, quote,
  announcement or uploaded image — and you must not feed it somebody else's
  confidential material expecting it to stay inside your server.

## 11. Third-party services

The Bot depends on services the operator does not control: **Discord** (the
platform itself, its API and its gateway), the public market endpoints named in
the Privacy Policy, **Supabase** (backups) and **Cloudflare** (the public site).
Your use of Discord is governed by your own agreement with Discord, and the
operator disclaims any warranty, support obligation or liability for how a third
party operates, authenticates, or processes data. Where the operator relies on a
third party to provide a feature, that feature may change or disappear with the
third party, and this is not a breach of these Terms.

## 12. Automated filters, automation and human review

Automod filters, level-role awards, activity tracking, scheduled posts and
market execution are **automated**, and they act without a person reading each
event first. None of them decides anything with a legal or materially
significant effect about you: they can hide a message, give a role, count a
minute, post a queue at its time, or fill a trade at a quoted price, and every
one of them can be switched off by an Administrator or by the operator.

If you want a human to review something the Bot did, ask in the support server
and say which server, channel and time. The operator will look, and will say
what was found. This is the human-intervention route described by the AI policy
and by the Privacy Policy, and it is the one to use for a decision you believe
was wrong rather than for a bug you can reproduce.

### 12.1 Security monitoring, and the notices it produces

The service is protected against automated abuse, request flooding and
unauthorised collection. One part of that protection is a **notice** the operator
receives when something noticeable happens — the Bot is added to a server, a
person joins one, a decoy page is fetched, or an automated client is refused
repeatedly. These notices travel over a channel that does not depend on the Bot
being connected, which is the point: a notice about an outage cannot be delivered
by the thing that is down.

**What a notice contains is closed and exhaustively listed in the Privacy Policy,
section 5** — the event's kind, the requesting address and client string, the path
and coarse country, the counts, and a server's name and identifier. It contains no
message, no member name, no content, and no credential, and it is transmitted
silently and rate-limited by the software itself. Where an address or client
string is processed this way it is for **security and abuse defence only**: it is
not used to profile you, not sold, not shared for advertising, and not used for
training of any kind. You must not attempt to defeat, evade or overload these
measures, and doing so is a breach of section 4 and may be reported to the
relevant provider or authority.

## 13. No professional reliance

Nothing the Bot produces is **medical, legal, financial, tax or other licensed
professional advice**, and it must not be relied on as a sole source of truth.
Market figures are numerical snapshots of public endpoints, not recommendations;
score, XP, level and economy figures are a game; moderation text explains what a
filter matched, not what happened. Do not use any Bot output about a person —
their activity, their messages, their standing — to decide anything with a legal
or material effect on them, including credit, employment, education, housing,
insurance, immigration or medical care. Where such a decision is being made,
make it yourself, with a person in the loop.

## 14. Anti-AI and automated collection

**Automated collection is prohibited, and training use is reserved against.**

* Do not crawl, scrape, mirror, bulk-download or systematically extract the
  Bot's website, the hub, the owner console, its rendered cards, its command
  output, or the contents of any Discord server through the Bot.
* Do not use anything you receive from the Bot or its website to train,
  fine-tune, distil, embed, benchmark or otherwise build a machine-learning
  model, or to construct a dataset for one — directly or through a third party.
  No licence to do so is granted, by implication or otherwise.
* Do not bypass a technical measure — a rate limit, a sign-in, a `robots.txt`
  directive, a response header, or a trap path described below — to obtain
  anything this section prohibits you from collecting.
* The prohibition is in three places, and they say the same thing: this section,
  the machine-readable `ai.txt` and `robots.txt` served by every host, and the
  reservation headers on every response (`X-Robots-Tag`, `Content-Usage`,
  `tdm-reservation`). Paths no person can reach and no page links to exist to
  identify collection that ignores all three; fetching one is evidence of it.
* **Enforcement.** The operator may block an address, network or country, and may
  treat collection as a breach of these Terms. Where a collector accessed
  anything behind a sign-in by defeating or ignoring that gate, the operator
  reserves every remedy available for unauthorized access to a computer system,
  in addition to the remedies for breach of contract. Where the collector is a
  commercial entity, the operator may also pursue the extraction itself.
* **Permission is available.** Partnerships, archives and research get written
  permission by asking in the support server first. A human reading a page,
  quoting it, linking to it or summarising it for somebody else is not
  collection and is welcome.

## 15. Indemnity

**You indemnify the operator** against claims brought by a third party that
arise from your use of the Bot: content you sent, permissions you granted,
decisions you took with its output, anything you did in breach of these Terms,
and anything you did in breach of section 14. **The operator indemnifies you**
against claims brought by a third party that the operator's own wilful breach of
these Terms caused.

**Procedure, so neither side is surprised.** The party seeking indemnity gives
prompt written notice; the indemnifying party controls the defence and may settle
its own way, except that it may not settle a claim in a way that admits your
fault or imposes a non-monetary obligation on you without your consent, and you
may take over the defence of a claim against you at your own cost. Both sides
cooperate honestly and share what they know. This section is **not** capped by
section 6 within the limits the law allows: an indemnity smaller than the cost of
answering a letter has no function.

## 16. Disputes, arbitration and class actions

**First, talk to the operator.** Write in the support server, say what happened
and what you want, and give it 30 days. Most disagreements here are about a
number in a database and end that way.

**Then, arbitration.** If that does not resolve it, any dispute arising out of
these Terms or the Bot is settled by **binding individual arbitration** rather
than in court, and you and the operator each waive trial by jury and the right to
bring or join a **class, collective or representative action**. The arbitration
is seated where the operator's principal place of business is, and where that is
India the Arbitration and Conciliation Act, 1996 governs the procedure. It is
confidential, documents are exchanged directly, and one arbitrator decides.

**What this does not waive.** You keep every right you have to bring a claim in a
small-claims court, to seek injunctive or other urgent relief for a breach of
section 14, and to complain to a regulator or supervisory authority. Nothing here
deprives a consumer of the mandatory protections of the law of their own
country of residence, and **you may opt out of this section entirely** by
writing to the operator in the support server within 30 days of first agreeing
to these Terms — one sentence, no reason needed. Opting out changes nothing else
about your access.

## 17. Notices, takedowns and the grievance officer

* **Copyright and other takedowns.** Send the address of the material, what it
  is, why the claim, and how to reach you, to the operator in the support server.
  Material the operator hosts will be removed or disabled while a claim is
  checked, and the person who posted it will be told, unless that would be
  unlawful. Knowingly false claims are the claimant's liability.
* **Grievance officer (India).** The operator designates a grievance officer,
  reachable through the support server, for complaints about the Bot as an
  intermediary and about personal data: the officer acknowledges a complaint
  within **7 days** and resolves it within **30 days**, and will say which
  extension applies and why if a case needs longer. The officer's name and
  working hours are available in the support server on request — they are not
  published here because that would publish one person's working hours to the
  whole internet.
* **Formal notices.** Legal notices are accepted through the support server,
  addressed to the operator, and are answered within 30 days.

## 18. Export controls and sanctions

Do not use the Bot, or access the console, from a country or in a way that
export-control or sanctions law prohibits, or on behalf of an entity subject to
such restrictions. The operator may geo-block, refuse a sign-in, or end access
where doing so is required or prudent, and this is not a breach of these Terms.

## 19. Changes and termination

These Terms may be revised; the Effective Date above moves with them, and the
revision digest recorded when you agreed (see the dashboard's agreement page)
tells you whether the text you accepted is the text in force. For a **material**
change the dashboard asks again — continuing to use the Bot is not treated as
acceptance of a clause you were never shown. The operator may suspend or end the
Bot, or terminate access, at discretion, and will say which of the following it
is where it can: a breach of these Terms, a legal requirement, a risk to the
platform or to other users, or a decision to stop running the Bot.

## 20. General

* **Severability.** If any provision is unenforceable, the rest stands, and the
  unenforceable one is read down to the minimum that is enforceable.
* **Entire agreement.** These Terms, the Privacy Policy and the AI & crawling
  policy are the whole agreement about the Bot, and they replace anything said
  about it in chat or in a support conversation.
* **No waiver.** Not enforcing a provision once is not a decision to abandon it.
* **Assignment.** You may not assign your rights under these Terms; the operator
  may assign them with the Bot, and the new operator takes on these Terms.
* **Contact.** Everything here is reachable through the bot's own support server,
  which is where a request, a complaint, a notice or an opt-out goes.
