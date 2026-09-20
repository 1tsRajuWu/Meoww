# Third parties the Bot talks to

Every outbound request the Bot makes, what leaves the host, and what happens if
you remove the service. The member-facing summary is section 5 of
[`PRIVACY.md`](../../PRIVACY.md); this is the operator's version, with the file
that makes each call.

Nothing here receives a Discord identity, a message, a member list or a token.
What leaves is a ticker symbol, a public URL, or bytes the Bot itself produced.

## Data stores

| Service | What it receives | Why | Where | To drop it |
|---|---|---|---|---|
| **Supabase Storage** | the whole `bot.db` file, plus up to 5 rotated snapshots, every ~25 seconds | backup and point-in-time restore | `utils/cloud_sync.py` | leave `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` unset — the Bot then runs with no off-host backup, so a lost container loses everything |

The bucket (`bot-backups`) is private: every read and write needs the
service-role key, which lives in the host's environment and never reaches the
dashboard or a Discord message. The payload is a raw SQLite file — the bucket's
privacy *is* the protection, so treat the key as the whole database's password,
and treat the bucket's access list as the list of people who can read it.

## Market prices

| Service | What it receives | Where | To drop it |
|---|---|---|---|
| **Binance** | a ticker symbol (`BTCUSDT`) | `utils/market.py` | the primary source; without it the market falls through to the next provider |
| **Kraken** | a ticker pair | `utils/market.py` | fallback |
| **Coinbase** | a ticker pair | `utils/market.py` | fallback |
| **TradingView** | a ticker symbol | `utils/market.py` | fallback, and the source of the chart page's candles |
| **CNBC** | a ticker symbol | `utils/market.py` | fallback |
| **Nasdaq** | a ticker symbol | `utils/market.py` | fallback |
| **Yahoo Finance** | a ticker symbol | `utils/market.py` | fallback |

Price failures **pause trading** rather than filling against a guessed number, so
with none of these reachable the market stops rather than lies. There is no
single switch: the providers are a chain.

## Fun content and images

| Service | What it receives | Where | To drop it |
|---|---|---|---|
| **Reddit** | a subreddit listing request (no user data) | `cogs/fun.py` | disable the `fun` module |
| **meme-api.com** | a meme lookup | `cogs/fun.py` | disable the `fun` module |
| **Tenor** | a search term for a GIF | `cogs/social.py` | disable the `social` module |
| **TheCatAPI** | a request for a random cat picture | `cogs/fun.py` | disable the `fun` module |
| **Spotify's oEmbed endpoint** | the Spotify link a member pasted | `utils/spotify.py` | the command simply will not resolve links |
| **unpkg.com**, then **cdn.jsdelivr.net** | the browser's request for the chart library | `templates/market_live.html` (allowed by the CSP in `keep_alive.py`) | this is the visitor's browser, not the host; the chart page needs one of them |

Images the Bot links (Imgur, Reddit's image hosts, Tenor media) are fetched by
**Discord's own client** when it renders the card, not by the Bot.

## Platform

| Service | What it receives | Where | To drop it |
|---|---|---|---|
| **Discord** | everything the Bot does — it is the platform | the whole project | nothing to drop; the Bot cannot exist without it |
| **Render** (or any host) | the process, its environment and its logs | `render.yaml` | self-host |
| **GitHub** | the source code. No server data is pushed there — the Gist backup path that once did is gone | repository only | — |

## What is *not* here

There is no analytics, no error-reporting service, no advertising SDK, no
customer-data platform, and no font or JavaScript CDN loaded by the dashboard
(the market chart page is the one CDN in the project).

**There are no AI runtime sub-processors:** no external LLM or AI APIs (such as
OpenAI, Anthropic, Google Vertex, or AWS Bedrock) receive Discord messages,
user prompts, guild metadata, or logs. While the Bot was built and is maintained
with the help of AI coding assistants and autonomous AI agents, the running
process executes purely deterministic Python code. No user data is ever sent to
an AI provider or used for model training.

If a future change adds any sub-processor, it belongs in this file and in
section 5 of the Privacy Policy in the same commit — `tests/test_legal.py` fails
if a host the code calls stops being named.
