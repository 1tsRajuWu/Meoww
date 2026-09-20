# Terms of Service

**Effective Date:** September 20, 2026 · **Version 4** (AI-assisted source).

By inviting, configuring or using this Discord bot ("the Bot") you agree to these
Terms. If you do not agree, remove the Bot from your server. They are between you
and the operator of the deployment you are using.

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
farm, or attack the Bot, its host, or upstream data sources. The operator may
reset balances, revoke access, or ban users and servers for abuse. Loopholes
closed after the fact may void associated gains.

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
Nothing here limits rights guaranteed by mandatory law.

## 7. Age

The Bot follows Discord's minimum age requirement. Do not use the Bot or
submit personal data if you do not meet Discord's age criteria.

## 8. AI assistance

**No part of the running Bot is AI** — no model is called, and nothing you send
reaches one. Created and maintained **with the help of AI coding assistants and
autonomous AI agents**.

## 9. Future subscriptions and operational fees

Core Bot functionality is currently offered without charge. To support ongoing
hosting, database maintenance, infrastructure scaling, and operational costs,
optional paid subscription tiers, server premium perks, or enhanced quotas may
be introduced in the future. Any future paid offerings will include explicit
pricing, billing schedules, and cancellation policies before purchase.
Introducing paid features will not forfeit or reset existing virtual progress.

## 10. Changes and termination

These Terms may be revised at any time; updates reflect in the Effective Date
above and continued use constitutes acceptance. The operator reserves the right
to suspend or discontinue the Bot or terminate access at discretion. If any
provision proves unenforceable, remaining terms remain in effect.
