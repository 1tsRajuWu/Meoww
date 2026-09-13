# Privacy Policy

**Effective Date:** September 14, 2026

This policy explains what this Discord bot ("the Bot") stores, why, where it
goes, and how to have it removed. By using a server that runs the Bot you agree
to it.

## 1. What we store

* **Identifiers:** Discord user, guild, channel, role and message IDs (message
  IDs only where a feature anchors to one, such as the starboard or a ticket).
* **Economy:** wallet and bank balances, inventory and active item effects, loans
  (amount, interest, due date, status), and market holdings and trade history
  (symbol, quantity, coins, timestamp).
* **Leveling:** XP and level, voice minutes actually paid for, and the level-role
  settings a server configured.
* **Moderation:** warnings and their reasons, and moderation-log settings. Cases
  are visible to server staff.
* **Server settings:** which features are enabled, ticket and starboard
  configuration, custom quotes a member chose to submit, and the like.
* **Content a member deliberately submits:** quote text, ticket subject and
  description, and announcements sent by staff. This text is stored because the
  feature is storing it.

## 2. What we do NOT store

* **Message text is not stored.** When the Message Content intent is enabled, the
  Bot reads messages in real time to detect invites, links and spam, and it
  counts length, characters and attachments to award XP. The message itself is
  discarded; only the counters and the timestamps persist. If a message is
  captured to the starboard or a mod-log entry, that copy is what the server
  asked for by configuring those features.
* **No voice audio is recorded or transmitted anywhere.** Voice activity is judged
  only from presence states Discord exposes (joined/left, mute, deafen, camera,
  live) and from duration.
* **No DMs are read, no personal information is requested**, and nothing is ever
  sold, rented or shared for advertising. There are no trackers and no
  analytics.

## 3. Where the data lives

* **The bot's own host** (a container with an **SQLite** database). This is the
  only place the data is live.
* **An encrypted off-site snapshot** — the database is encrypted with a key the
  Bot derives from a private backup token, then uploaded to a **private** GitHub
  Gist for disaster recovery. The Gist alone is not readable without that key,
  and the Bot refuses to overwrite a snapshot it cannot read.
* **The dashboard** — a web page served by the Bot's host, guarded by a passkey,
  a session and CSRF protection. Whoever holds the passkey (server staff) can see
  and change that server's settings and data.

## 4. Third parties the Bot talks to

The Bot fetches public content and prices. **None of these requests carry your
identity** — they are a ticker symbol, a public link or nothing at all:

* **Market prices:** Binance, Kraken, Coinbase, TradingView, CNBC, Yahoo Finance
  and Nasdaq. Only the symbol is sent (for example `BTC`).
* **Fun content:** Reddit and meme-api (memes), Tenor (GIFs), TheCatAPI (cat
  pictures), and Spotify's public oEmbed endpoint, which receives the Spotify
  link a member pasted.
* **Backups:** GitHub's API, using the operator's token.
* **Dashboard styling:** Google Fonts.

Deleting the Bot from a server stops all of this for that server.

## 5. Retention, and how to have data removed

Data is kept while the Bot is in your server, because that is the feature.
Server staff can delete specific data at any time with the Bot's own commands —
for example `/clearwarnings` for a member's warnings and `/eco-admin reset` for
balances — and the operator can delete a whole server's data or hand over the
database on request. Encrypted backups rotate: only the most recent snapshots
are kept. If you remove the Bot, ask the operator to delete the server's rows.

## 6. Children, changes and contact

The Bot is not directed at anyone below Discord's minimum age. This policy may be
updated at any time; the Effective Date will change. Questions, removal requests
or anything about your data: contact the Bot operator through the server where
you use it.
