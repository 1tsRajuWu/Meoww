# Terms of Service

**Effective Date:** September 19, 2026 · **Version 3** (adds the dashboard's
Discord sign-in).

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
provided **"as is" and "as available"**, with no warranty of any kind, and it may
change, break or be discontinued at any time.

## 2. Your responsibilities as staff

* You decide who gets each permission. The Bot enforces what Discord tells it and
  what the operator configured; it cannot tell whether *your* staff should have
  those powers. Configure roles deliberately.
* Commands that change data — moderation, role assignment, economy admin, level
  roles, module toggles, announcements, the market's role — act on your
  instruction, and so does everything sent from the dashboard. The composer
  speaks **as the Bot**: what it posts is your message, and a scheduled one goes
  out later on its own. The operator is not responsible for what your staff or
  members send with it.
* Keep the dashboard passkey and any sign-in link private. Access is the
  credential, so anything done with it is treated as your instruction. Sign-in
  links are single use and expire in 15 minutes. Where "Sign in with Discord" is
  enabled, your own Discord account is the credential and your own permissions
  decide what you may change: only servers where you hold Manage Server or better
  are offered, and the session can reach no others — so removing somebody's roles
  in Discord removes their access here.
* Scheduled messages depend on the host being up. A post more than 24 hours late
  is skipped and says so rather than arriving at the wrong time, and a repeat
  stops after three failed attempts.
* The Bot is not a substitute for human moderation, and automated filters can be
  wrong in both directions. Review what it does.

## 3. Virtual currency, items and the market

* **Coins, items, loans and market positions are virtual.** They have no cash
  value, cannot be exchanged for money or anything outside the Bot, and confer no
  rights against the operator. Nothing in the Bot is a financial product or
  advice.
* **The market quotes real prices** from public exchange and market-data
  endpoints (see the Privacy Policy for the list). Trades settle at the observed
  price, 1:1, in virtual coins: real-world price moves are reproduced, the coins
  are not.
* **Quotes can be delayed, wrong or unavailable.** When no source can be reached,
  trading pauses rather than filling an order against a guessed price, and a
  price older than the freshness window cannot be traded at all.
* **Limits apply and may change:** a minimum and maximum size per fill, a rolling
  daily ceiling on what the market pays one member, and a cooldown between fills.
  Access is limited to the role the operator chose.
* Prices come from third parties the operator does not control, and the operator
  is not liable for their accuracy, availability, or for losses in virtual coins
  that follow from them.
* A loan is a virtual debt with in-Bot consequences only — interest, late
  penalties, blocked borrowing. It is never a real debt.

## 4. Acceptable use

Do not use the Bot to break Discord's Terms of Service or Community Guidelines,
or any law. Do not exploit bugs, races, loopholes or scripted input to obtain
coins, XP, items, roles or access you were not meant to have; report them
instead. Filing a bug report you know to be false, or spamming the report and
feedback tools, is itself misuse. Do not spam, evade cooldowns, farm with
alternate accounts, or attack the Bot, its host or its third-party data sources.
The operator may reset balances, levels or settings, revoke access, or block a
user or server at any time, particularly where abuse or a bug is involved.
Loopholes closed after the fact may invalidate gains made through them.

## 5. Availability, backups and data loss

The Bot runs on a hosting service and may be offline for deploys, restarts or
host limits. Progress is stored in an SQLite database that restores from a
private Supabase backup on boot and re-saves roughly every 25 seconds, with a few
hourly snapshots kept for recovery — but **no backup is guaranteed**, and data
older than the retention windows is pruned by design (365 days of activity, 90
days of dashboard message history). That database is a file the operator can
read, so nothing should be sent to the Bot that the operator may not see. See the
Privacy Policy for what is stored and how to have it deleted.

## 6. Limitation of liability

To the maximum extent permitted by law, the operator is not liable for any
direct, indirect, incidental or consequential damage arising from the Bot,
including lost data, lost virtual progress, server disruption or moderation
outcomes. Where liability cannot be excluded, it is limited to nothing, because
the Bot is free. Nothing here limits a right that the law does not allow to be
limited.

## 7. Age

The Bot follows Discord's minimum age for its service. Do not use the Bot if you
are below it, and do not ask the Bot to store anything about someone who is.

## 8. Changes and termination

These Terms may be updated at any time; the Effective Date above changes with it
and continued use is acceptance. The operator may suspend or end the Bot's
service, or any server's access to it, at any time. If a clause here turns out to
be unenforceable, the rest still stands.
