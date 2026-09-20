# Privacy Policy

**Effective Date:** September 19, 2026 · **Version 3** (adds Discord sign-in for
the dashboard).

This policy explains what this Discord bot ("the Bot") stores, why, where it goes
and how to have it removed. It describes one deployment; the operator running it
answers for it, and using a server that runs the Bot is agreement to it.

## 1. What we store

* **Identifiers:** Discord user, guild, channel, role and message IDs (a message
  ID only where a feature anchors to one).
* **Economy:** wallet and bank balances, inventory and item effects, loans
  (amount, interest, due date, status), market holdings and trades (symbol and
  quantity).
* **Leveling:** XP, level, messages counted, voice minutes paid for, coins
  earned, and the server's level-role settings.
* **Activity Tracker:** per-day, per-channel message counts and voice seconds per
  member; rows older than 365 days are deleted automatically.
* **Moderation:** warnings, cases with reasons, and automod configuration —
  visible to server staff.
* **Tickets:** ticket configuration, the transcript a closed ticket produces, and
  the rating and comment a member leaves.
* **Server settings:** enabled features, house-channel, log, starboard and market
  configuration, and social data members opted into (marriages, zoo, quotes).
* **Text a person deliberately submits:** quote text, a ticket subject and
  description, a bug report, a moderation reason, and dashboard announcements,
  including **scheduled** ones, with their text, channels, repeat rule and who
  queued them.

## 2. What we do NOT store

* **Message text is not stored** to award XP or filter spam. With the Message
  Content intent on, the Bot reads a message in the moment for invites, links,
  length and attachments, then discards it; only counters and timestamps persist.
* **A copy exists only where a server asked for one:** a starboard pin, a
  mod-log entry, a ticket transcript, or submitted text.
* **No voice audio is recorded or transmitted.** Voice activity is judged only
  from the presence states Discord exposes (joined/left, mute, deafen, camera,
  live).
* **No DMs are read**, and nothing is sold, rented or shared for advertising. No
  trackers, no analytics. The Bot requests no profile information itself — the
  exception below is optional and exists only so a server's administrators can
  reach the dashboard.
* **Signing in to the dashboard with Discord** shares your user id, name, avatar
  and the servers you are in with your permissions there, used only to list  them and decide which you may administer (Manage Server or above). The access
  token is discarded in that same request and never stored, and nothing about you
  reaches the database — what stays is your own session cookie, for up to 8
  hours. Scopes: `identify` and `guilds`. Off unless the operator configures it.
* The dashboard's login rate limiter counts failed attempts **per source address
  in the host's memory only** — never in the database, gone on restart.

## 3. Where the data lives

* **The bot's own host**, as an SQLite database — the only live copy.
* **A private Supabase Storage bucket** (`bot-backups`): the live `bot.db` plus
  up to 5 hourly snapshots for point-in-time restore, re-saved about every 25
  seconds and restored on boot. Every read and write needs the operator's
  service-role key, and the payload is the raw database file.
* **The dashboard**, served by the bot's host, with three ways in: a passkey, a
  one-time Discord link (single use, 15 minutes, in memory), and "Sign in with
  Discord" when configured. All end in one `meow_session` cookie (HttpOnly,
  SameSite, Secure on HTTPS, up to 8 hours) plus CSRF protection. A one-time link
  is scoped to the server it was made in; a Discord sign-in reaches only the
  servers that person administers. Staff with access, and the operator, can see
  and change that server's data.

## 4. A bug report can leave your server

A bug report is posted in **your** server's bug channel when it has one, with a
thread on it. When it does not, the Bot posts it to **the bot's own server**
rather than dropping it — so the report text, the reporter's mention and the
server's name become visible to the operator. Nothing else crosses between
servers, and no feature reads another server's data.

## 5. Third parties the Bot talks to

A request carries no identity beyond what any request must; the operator chose
these services and cannot see what they do with what they receive.

* **Market prices:** Binance, Kraken, Coinbase, TradingView, CNBC, Nasdaq and
  Yahoo Finance — the ticker symbol only, for example `BTC`.
* **Fun content:** Reddit and meme-api (memes), Tenor (GIFs), TheCatAPI (cat
  pictures), and Spotify's public oEmbed endpoint, which receives the pasted link.
* **The market chart page:** the visitor's browser loads the chart library from
  **unpkg.com**, falling back to **cdn.jsdelivr.net**, so those hosts see the
  visitor's IP and browser.
* **Backups:** Supabase's API, using the operator's service-role key.
* **This website:** Cloudflare serves it, so it sees the visitor's IP.

## 6. Retention, and how to have data removed

Data is kept while the Bot is in your server, because that is the feature. Two
things expire on their own: Activity Tracker rows after **365 days**, and
dashboard message history (what was queued, sent, and the per-channel result)
after **90 days**. Snapshots rotate, keeping the newest 5.

Staff delete data with the Bot's own commands (`/clearwarnings`, `/eco-admin
reset`, and the ticket, quote and case tools); the operator can delete or export
a whole server's rows on request; removing the Bot stops all further collection.
A snapshot's records persist until it rotates out, and a retired feature's rows
are included in any deletion. Ask in the support server and the procedure —
which has two traps in it — will be run for you.

## 7. Children, changes and contact

The Bot is not directed at anyone below Discord's minimum age, and asks for no
personal information. This policy may change; the Effective Date above moves
with it, and an earlier version is available on request. Contact the operator
through the server where you use the Bot.
