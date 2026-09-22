# Privacy Policy

**Effective Date:** September 21, 2026 · **Version 5** (AI-assisted source).

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
  visible to server staff. A warning given from the website is the same row a
  `/warn` produces, with the same reason text.
* **Tickets:** ticket configuration, the transcript a closed ticket produces, and
  the rating and comment a member leaves.
* **Server settings:** enabled features, house-channel, log, starboard and market
  configuration, and social data members opted into (marriages, zoo, quotes).
* **Text a person deliberately submits:** quote text, a ticket subject and
  description, a bug report, a moderation reason, and dashboard announcements,
  including **scheduled** ones, with their text, channels, repeat rule and who
  queued them.
* **Anonymous Q&A:** the question text, the answer given to it, and — written
  beside them — **who asked**, as their Discord id and display name. The
  question itself is posted to the server's chosen channel by a webhook, so no
  member's name, mention or profile is on it: the channel sees the question and
  the asker sees that it was received. The name is rendered in exactly two
  places, both of which refuse anyone who is not the bot's owner — the one
  account this deployment is configured with, which is the operator rather than
  any administrator holding a permission: a DM to that account, and the
  `/qna inbox` list. The name of the setting that points at it belongs in the
  operator's own configuration reference rather than here, because this page is
  public. The answer
  is delivered to the asker by DM and is never posted. There is no expiry on
  these rows — a question is kept until it is deleted, because an unanswered one
  is the feature.
* **What was done through the website, and by whom:** one row per action taken
  from the dashboard or the operator's console — the scope, the person's Discord
  id and username, the server, the action, who or what it was aimed at, whether
  it worked, and the reason given. This exists because **Discord's own audit log
  credits the Bot, not the person**: the Bot is what performs a kick or a
  lockdown, so without this row there is no record of who asked for one. It is a
  moderation record, kept for the same reason cases are.
* **A lockdown's undo.** Locking a server down changes every channel's
  permissions, so what each channel had *before* is written down — channel id and
  the two permission numbers — for exactly as long as the lockdown stands, so
  that lifting it restores what was there rather than granting something new.
* **A blocklist row, where the operator has refused a server:** the server id, the
  reason, when, and which operator signed in when it happened. No members are in
  it.

## 2. What we do NOT store

* **Message text is not stored** to award XP or filter spam. With Message
  Content on, the Bot inspects invites, links, length and attachments in the
  moment, then discards the text; only counters and timestamps persist.
* **A copy exists only where a server asked for one:** a starboard pin, a
  mod-log entry, a ticket transcript, or submitted text.
* **No voice audio is recorded or transmitted.** Voice activity is judged only
  from the presence states Discord exposes (joined/left, mute, deafen, camera,
  live). No audio is used for training anything, ever.
* **No music is stored, and nothing is kept about who asked for what.** A track is
  streamed from the platform that hosts it straight into the voice channel; the
  bot keeps no copy, writes no queue to the database, and remembers no request
  history — the queue lives in memory and a restart empties it. The one thing
  stored is a per-server preference: which voice channel a server asked to keep
  occupied, if it asked at all.
* **No DMs are read**, and nothing is sold, rented or shared for advertising. No
  trackers, no analytics. The Bot requests no profile information itself — the
  exception below is optional and exists only so a server's administrators can
  reach the dashboard.
* **Signing in to the dashboard with Discord** shares your user id, name, avatar
  and the servers you are in with your permissions there, used only to list them and decide which you may administer (Administrator, or owner). The access
  token is discarded in that same request and never stored, and nothing about you
  reaches the database beyond the sign-in row above and your own session cookie,
  for up to 8 hours. Scopes: `identify` and `guilds`. Off unless the operator
  configures it.
* The dashboard's login rate limiter counts failed attempts **per source address
  in the host's memory only** — never in the database, gone on restart.
* **Your data does not train a model.** Nothing you send, nothing the Bot stores
  and nothing it renders is used to train, fine-tune or evaluate a machine
  learning model, and it is not sent anywhere that does. There is no second
  pipeline, because there is no training pipeline at all — the running Bot calls
  no model of any kind, which section 7 states as a fact about this codebase
  rather than as a promise.

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
* **The agreement record.** Agreeing to the policies writes one row: the revision
  digest of the three documents, the scope and Discord id the session carried,
  the address and browser the agreement came from, and the time. It is kept for
  three years, which is longer than anything else here, because it is the one
  record that answers a question asked *after* an account is gone.
* **Security, stated plainly.** The database file is not encrypted at rest by
  this project; the bucket holding it is private and its key is the operator's.
  The connection to the dashboard is HTTPS. Every account that can reach the
  database is an account that can read it, and the number of those is small on
  purpose. Vulnerabilities go to the process in `SECURITY.md`.

## 4. A bug report can leave your server

A bug report is posted in **your** server's bug channel when configured. When
not, the Bot posts it to **the bot's own server** rather than dropping it — so
the report text, the reporter's mention and the server's name become visible to
the operator. Nothing else crosses between servers.

## 5. Third parties the Bot talks to

A request carries no identity beyond what any request must; the operator chose
these services and cannot see what they do with what they receive.

* **Market prices:** Binance, Kraken, Coinbase, TradingView, CNBC, Nasdaq and
  Yahoo Finance — the ticker symbol only, for example `BTC`.
* **Fun content:** Reddit and meme-api (memes), Tenor (GIFs), TheCatAPI (cat
  pictures), and Spotify's public oEmbed endpoint, which receives the pasted link.
* **Music:** YouTube and SoundCloud receive **the words somebody typed** as a
  search, or the link they pasted, from this host's address — and the audio then
  streams from that platform into the voice channel, never through the operator.
  Where the operator has configured an **outbound proxy** for these providers —
  which is one of the ways a platform that has decided a datacenter address is a
  bot gets answered — the search and the audio fetch both leave from that proxy's
  address instead, so the operator's proxy provider sees that same query and that
  same audio request in place of this host, and handles it under its own policy.
  What is sent does not change with it: the same words, the same link, no account
  and no identifier, and a proxy is a technical relay rather than a party to
  anything. A search is a query and not an account: the bot has no profile on either
  platform, and nothing links a request to a person. `/music songinfo` sends a
  title, or an artist and title, to Apple's iTunes search API, `/music lyrics`
  sends the same pair to `api.lyrics.ovh`, and a radio station (SomaFM, 
  ilovemusic, plaza.one, listen.moe, zeno.fm and whatever else a deployment 
  configures) sees the request for its stream — which is this host's address and 
  nothing about anybody in the channel.
* **The market chart page:** the visitor's browser loads the chart library from
  **unpkg.com**, falling back to **cdn.jsdelivr.net**, so those hosts see the
  visitor's IP and browser.
* **Backups:** Supabase's API, using the operator's service-role key.
* **This website:** Cloudflare serves it, so it sees the visitor's IP.
* **Security notices:** a private Discord webhook the operator configures, used
  for the notifications that have no other route — the Bot was just added to a server (so
  it is in none of that server's channels), the Bot is unreachable, or the
  website is being flooded by an automated client. What such a notice contains is
  **closed and exhaustively listed**: the kind of event; the requesting
  **IP address** and the **User-Agent string** it presented, which names an
  operating system and a browser; the **URL path** requested and the coarse
  **country** Cloudflare reports; the **counts** that make the event legible (how
  many times, how many members, how old an account is); and, when the event is
  about a server, that server's **name and identifier**. Nothing else — no
  message, no member name, no email, no token, no cookie, no content of any kind.
  It is sent **silently** (it does not notify anybody's device), at most once per
  kind of event every 30 seconds, and only when something noticeable has happened;
  while the setting is unset, nothing is transmitted and the dashboard reports
  that as a missing setting rather than as silence. This is deliberately narrow
  because its purpose is to let the operator recognise abuse **as it begins**
  rather than after it ends, and the list above is enforced by the software's own
  test suite, not only by this paragraph.
* **What none of them get:** an account, a message, a transcript, or a name of
  yours that is not already inside the thing being sent. The operator's list of
  sub-processors, with what each one receives and how to drop one, is the
  operator's own document — ask and it will be shared.

## 6. Retention, and how to have data removed

Data is kept while the Bot is in your server, because that is the feature. Four
things expire on their own: Activity Tracker rows after **365 days**, dashboard
message history (what was queued, sent, and the per-channel result) after **90
days**, dashboard sign-in rows once they expire or are revoked, and a lockdown's
undo record the moment the lockdown is lifted. `guild_blocklist` and
`web_actions` are read by the operator and are included in any deletion or export
asked for under section 9.

Staff delete data with the Bot's own commands (`/clearwarnings`, `/eco-admin
reset`, and the ticket, quote and case tools); the operator can delete or export
a whole server's rows on request; removing the Bot stops all further collection.
A snapshot's records persist until it rotates out, and a retired feature's rows
are included in any deletion. Ask in the support server and it will be done for
you.

## 7. AI

**No part of the running Bot is AI** — no model is called. Created and maintained
**with the help of AI coding assistants and autonomous AI agents**, and the
disclosure is in the Terms as well as here. What the deployment does about
*other people's* automated reading of it — crawling, training use, and the pages
that identify both — is the AI & crawling policy, which is the third document in
the same bundle.

## 8. Why we are allowed to hold it, and for how long

| What | Why | Basis |
|---|---|---|
| Counters, levels, economy, starboard, tickets | the feature itself, for the server that asked for it | performance of the agreement you are party to |
| Activity and voice counters | the server's own activity view | the same, and a legitimate interest in a feature staff turned on |
| Moderation cases, warnings | so a server can keep a record of its own mod actions | the same |
| Bug reports, quote text, sticky settings | because somebody deliberately submitted them | consent, which is why submitting them is how they got there |
| Sign-in rows and agreement records | to know who is signed in and what they agreed to | legitimate interest in the security of the deployment, and in being able to prove the agreement |
| Abuse signals about requests to the website — address, client string, path, country, counts (section 5, *Security notices*) | to identify and repel automated abuse, denial-of-service attempts and unauthorised collection as they happen, and to keep a short record that one occurred | legitimate interest in the integrity and availability of the service; a request that is refused is not consented to, and the data is limited to what identifying and repelling the request requires |
| Personal data from the European Union, the United Kingdom, India or California | the same purposes, with the rights in section 9 | the GDPR/UK GDPR bases above; India's DPDP Act 2023, where the operator is the Data Fiduciary and consent is itemised and never bundled; the CCPA/CPRA, under which nothing is sold or shared as those terms are defined |

Two rules from that table, applied everywhere: **data is collected for the
feature that needs it and not for a later purpose**, and **consent is never
bundled** — agreeing to a policy, buying something, or using a command is never
also agreement to a second thing.

## 9. Your rights, and how to use them

If any of the laws below applies to you, you have the rights they give you, and
the operator honours them for everybody who asks anyway:

* **See it** — a copy of what this deployment holds about you or about a server
  you administer.
* **Correct it** — wrong counts, a stale label, an inaccurate case reason.
* **Delete it** — in whole or in part, including a single channel's rows.
* **Take it** — an export in a machine-readable form.
* **Object or restrict** — including turning a feature off, which stops the
  collection at its source.
* **Withdraw consent** — for anything held on consent, without losing access to
  anything that does not depend on it.
* **Complain to a regulator** — including your national data-protection
  authority, and in India the Data Protection Board.

**How to ask.** Ask in the bot's own support server: say which server, which
member or which channel, and what you want done. The operator answers within
**30 days**, will say if a case needs longer and why, and does not charge for a
first request. Refusing a request is answered with the reason and with how to
appeal. From India, the same route reaches the **grievance officer** described in
the Terms: acknowledged within 7 days, resolved within 30.

**Two honest limits.** A snapshot taken before a deletion can keep a record until
it rotates out — the operator will say which one holds it and when it expires
rather than pretend it is gone. And a record that a *moderation* action happened
may survive a deletion request where the law or the server's own duty requires
the log to stay; what that log contains is the fact of the case, not its text.

**Nothing here is decided for you by a machine.** You will never be refused,
banned or limited by an automated decision with a legal or similarly significant
effect that has no human behind it: filters and market execution act on messages
and orders, and a ban, a reset or an access revocation is a person's decision.
Where you want a human to look at something the Bot did, ask in the support
server — section 12 of the Terms is that route, and it is answered.

**Cookies, and why there is no consent banner.** The only cookie this project
sets is the dashboard's `meow_session` cookie, which is strictly necessary to
stay signed in; nothing else is set, nothing is read back from anywhere else, and
there is no advertising or analytics cookie of any kind. A banner asking you to
consent to something that does not exist would be theatre, so there is none. If a
non-essential cookie is ever added, it will be asked for separately, refused by
default, and as easy to decline as to accept.

## 10. Where it is processed, and where it goes

The Bot's host and the Supabase bucket holding the backups are the operator's
chosen providers and may be in a different country from you; Cloudflare's network
serves the public site from wherever is nearest to the visitor. Where the operator
has configured an outbound proxy for the media providers (section 5), that relay is
an operator-chosen processor as well and may be in a further country, under the same
safeguards. Where personal
data moves out of the European Economic Area, the United Kingdom or India, the
transfer relies on the provider's own safeguards — standard contractual clauses,
an adequacy decision, or the equivalent — and the operator will say which one
applies to a given store on request. Data is not transferred to anybody else, for
any purpose, and in particular not to a model trainer.

## 11. Children, changes and contact

The Bot is not directed at anyone below Discord's minimum age, and asks for no
personal information. This policy may change; the Effective Date above moves
with it, an earlier version is available on request, and a **material** change
means the dashboard asks for agreement again rather than counting continued use
as consent. Contact the operator through the server where you use the Bot.
