# Privacy Policy

**Effective Date:** September 18, 2026 · **Version 2** (supersedes the
September 16, 2026 version, which predates the composer, scheduled messages,
bug reports and the backup host described below).

This policy explains what this Discord bot ("the Bot") stores, why, where it goes
and how to have it removed. It describes one deployment: the operator running it
answers for it. Using a server that runs the Bot is agreement to it.

## 1. What we store

* **Identifiers:** Discord user, guild, channel, role and message IDs (a message
  ID only where a feature anchors to one).
* **Economy:** wallet and bank balances, inventory and active item effects, loans
  (amount, interest, due date, status), market holdings and trade history
  (symbol, quantity, coins, timestamp).
* **Leveling:** XP, level, messages counted, voice minutes paid for, coins
  earned, and the level-role settings a server configured.
* **Activity Tracker:** per-day, per-channel message counts and voice seconds per
  member. Rows older than 365 days are deleted automatically.
* **Moderation:** warnings, moderation cases and their reasons, and automod
  configuration. These are visible to server staff.
* **Tickets:** ticket configuration, the transcript a closed ticket produces, and
  the rating and comment a member chooses to leave.
* **Server settings:** which features are enabled, house-channel, log, starboard
  and market configuration, and social data members opted into (marriages, zoo,
  quotes).
* **Text a person deliberately submits:** quote text, a ticket subject and
  description, a bug report, a moderation reason, and announcements composed in
  the dashboard — including ones **scheduled** to post later, stored with their
  text, target channels, repeat rule and who queued them.

## 2. What we do NOT store

* **Message text is not stored** to award XP or to filter spam. With the Message
  Content intent on, the Bot reads a message in the moment for invites, links,
  length and attachments; it is then discarded, and only counters and timestamps
  persist.
* **A copy exists only where a server asked for one:** a starboard pin, a
  mod-log entry, a ticket transcript, or text a person submitted.
* **No voice audio is recorded or transmitted.** Voice activity is judged only
  from the presence states Discord exposes (joined/left, mute, deafen, camera,
  live).
* **No DMs are read, no profile information is requested**, and nothing is sold,
  rented or shared for advertising. No trackers, no analytics.
* The dashboard's login rate limiter counts recent failed attempts **per source
  address in the host's memory only** — never written to the database, and gone
  when the process restarts.

## 3. Where the data lives

* **The bot's own host**, as an SQLite database. This is the only place the data
  is live.
* **A private Supabase Storage bucket** (`bot-backups`): the live `bot.db` plus
  up to 5 hourly snapshots for point-in-time restore. The Bot restores the newest
  usable backup on boot and re-saves about every 25 seconds; every read and write
  needs the operator's service-role key, and the payload is the raw database
  file — the bucket is private rather than the file encrypted.
* **The dashboard**, served by the bot's host, guarded by a passkey, a one-time
  Discord sign-in link (single use, 15 minutes, held in memory), a session cookie
  and CSRF protection. The cookie is `meow_session`: HttpOnly, SameSite, Secure
  on an HTTPS host, up to 8 hours. Staff who hold access, and the operator, can
  see and change that server's data.

## 4. A bug report can leave your server

A bug report is posted in **your** server's bug channel when it has one, with a
thread on it. When it does not, the Bot posts it to **the bot's own server**
rather than dropping it — so the report text, the reporter's mention and the
server's name become visible to the operator. Nothing else crosses between  servers: release notes and failure reports stay where they belong, and no
  feature reads another server's data.

## 5. Third parties the Bot talks to

A request carries no identity beyond what any request must. The operator chose
these services, and the Bot cannot see what they do with what they receive.

* **Market prices:** Binance, Kraken, Coinbase, TradingView, CNBC, Nasdaq and
  Yahoo Finance — the ticker symbol only (for example `BTC`).
* **Fun content:** Reddit and meme-api (memes), Tenor (GIFs), TheCatAPI (cat
  pictures), and Spotify's public oEmbed endpoint, which receives the pasted
  Spotify link.
* **The market chart page:** the visitor's browser loads the chart library from
  **unpkg.com**, falling back to **cdn.jsdelivr.net**, so those hosts see the
  visitor's IP address and browser, as any web asset does.
* **Backups:** Supabase's API, using the operator's service-role key.
* **Code and documents:** GitHub serves the source code and these documents.

## 6. Retention, and how to have data removed

Data is kept while the Bot is in your server, because that is the feature. Two
things expire on their own: Activity Tracker rows after **365 days**, and the
dashboard's message history — what was queued, what was sent, the per-channel
result — after **90 days**. Cloud snapshots rotate, keeping the newest 5.

Staff delete data with the Bot's own commands (`/clearwarnings`, `/eco-admin
reset`, and the ticket, quote and case tools); the operator can delete or export
a whole server's rows on request; removing the Bot stops all further collection.
Records already in a snapshot persist until it rotates out; rows a retired
feature left behind are included in any deletion. The procedure is in
`docs/legal/data-requests.md`.

## 7. Children, changes and contact

The Bot is not directed at anyone below Discord's minimum age, and it asks for no
personal information. This policy may change at any time; the Effective Date
changes with it and every version stays in the repository. Contact the operator
through the server where you use the Bot.
