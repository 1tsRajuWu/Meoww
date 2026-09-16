# Terms of Service

**Effective Date:** September 16, 2026

By inviting, configuring or using this Discord bot ("the Bot") you agree to these
Terms. If you do not agree, remove the Bot from your server.

## 1. What the Bot is

The Bot is a self-hosted community bot: moderation and logging, automod filters,
tickets with close-time ratings, a leveling system with XP and automatic role
rewards, a coin economy with a shop, loans and casino games, a live market,
a starboard, social roleplay, fun commands, mini-games, a per-channel activity
tracker, and a web dashboard for server staff (module toggles, announcement
composer, live market charts). It is provided **"as is" and "as available"**,
with no warranty of any kind, and it may change, break or be discontinued at
any time.

## 2. Your responsibilities as staff

* You decide who gets each permission. The Bot enforces what Discord tells it
  and what the operator configured; it cannot tell whether *your* staff should
  have those powers. Configure roles deliberately.
* Commands that change data — moderation, role assignment, economy admin, level
  roles, module toggles, announcements, the market's role — act on your
  instruction. The operator is not responsible for actions your staff or members
  take with them.
* Keep the dashboard passkey and any sign-in link private. Anyone holding them
  can change this server's settings and read its data.
* The Bot is not a substitute for human moderation, and automated filters can be
  wrong in both directions. Review what it does.

## 3. Virtual currency, items and the market

* **Coins, items, loans and market positions are virtual.** They have no cash
  value, cannot be exchanged for money or anything outside the Bot, and confer
  no rights against the operator. Nothing in the Bot is a financial product or
  advice.
* **The market quotes real prices** from public exchange and market-data
  endpoints (see the Privacy Policy for the list). Trades settle at the observed
  price, 1:1, in virtual coins. Real-world price moves are reproduced; the coins
  are not.
* **Quotes can be delayed, wrong or unavailable.** When no source can be reached,
  trading pauses rather than filling an order against a guessed price. A price
  older than the freshness window cannot be traded at all.
* **Limits apply and may change**: a minimum and maximum size per fill, a rolling
  daily ceiling on how many coins the market pays one member, and a cooldown
  between fills. Access to the market is limited to the role the operator chose.
* Prices come from third parties. The operator does not control them and is not
  liable for their accuracy, availability or for losses (in virtual coins) that
  follow from them.
* A loan in the Bot is a virtual debt with in-Bot consequences only: interest,
  late penalties and blocked borrowing. It is never a real debt and creates no
  obligation outside the Bot.

## 4. Acceptable use

Do not use the Bot to break Discord's Terms of Service or Community Guidelines,
or any law. Do not exploit bugs, races, loopholes or scripted input to obtain
coins, XP, items, roles or access you were not meant to have; report them
instead. Do not spam, evade cooldowns, farm with alternate accounts, or attack
the Bot, its host or its third-party data sources. The operator may reset
balances, levels or settings, revoke access, or block a user or server at any
time, particularly where abuse or a bug is involved. Loopholes closed after the
fact may invalidate gains made through them.

## 5. Availability, backups and data loss

The Bot runs on a hosting service and may be offline for deploys, restarts or
host limits. Progress is stored in an SQLite database that restores from a
private Supabase backup on boot and re-saves roughly every 25 seconds, with a
few hourly snapshots kept for recovery — but **no backup is guaranteed**, and
activity data older than 365 days is pruned by design. Refer to the Privacy
Policy for what is stored and how to have it deleted.

## 6. Limitation of liability

To the maximum extent permitted by law, the operator is not liable for any
direct, indirect, incidental or consequential damage arising from the Bot,
including lost data, lost virtual progress, server disruption or moderation
outcomes. Where liability cannot be excluded, it is limited to nothing, because
the Bot is free.

## 7. Changes and termination

These Terms may be updated at any time; the Effective Date above will change and
continued use is acceptance. The operator may suspend or end the Bot's service,
or any server's access to it, at any time.
