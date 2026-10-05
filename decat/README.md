# 🛰️ Decat — the half that runs on your Mac

`/decat` takes a question in Discord, relays it to **this computer**, answers it
with a model running here, and posts the answer back with the pages it read.
Nothing about a question is sent to a model vendor, because the model is not a
service — it is a program on this machine.

```
Discord ──/decat──► the bot's host ──poll──► this Mac
                     (keeps nothing)          ├─ Ollama: qwen3-vl:8b  (local)
                                              ├─ search + read pages
                                              ├─ answer, then check it
                                              └─ answer back ──► the Discord card
```

## Install it once

```bash
bash decat/mac/install.sh
```

It installs Ollama (Homebrew), pulls `qwen3-vl:8b` (6.1 GB, once), writes
`~/.decat/agent.env`, installs the `decat` command, and registers a login item so
it starts at login and restarts if it stops. Then it runs `decat doctor` and tells
you what is still missing.

Two things must match before a question can be answered:

1. **`DECAT_TOKEN` in `~/.decat/agent.env`** must equal the value the host has
   (or leave it blank there and here, and the host's `ADMIN_PASSKEY` is used).
2. **The server must have the system switched on** — an administrator, once.

## The switch

* **`http://127.0.0.1:8787`** — one button, plus the facts you actually want:
  whether Ollama is up, whether the model is pulled, whether the host answers,
  whether the search engine does. Bound to loopback, so nothing on your network
  can reach it and it holds no credential.
* **`decat on`** / **`decat off`** / **`decat status`** in a terminal.

While it says **off** the machine keeps polling and keeps saying "off", so the bot
answers *"the Mac's switched off rn"* instead of *"offline"* — a switch is only
worth having if the difference is visible from Discord.

## `~/.decat/agent.env`

| Setting | Meaning |
|---|---|
| `DECAT_TOKEN` | the credential the host expects (see above) |
| `DECAT_SPACE` | where the bot lives |
| `DECAT_MODEL` | the model. `qwen3-vl:8b` is the default: vision, tool-capable, 256K context, 6.1 GB, Apache-2.0 |
| `DECAT_FAST_MODEL` | the lane for short questions with no picture (`qwen3-vl:4b`, 3.3 GB). Blank means "same as the big one" |
| `DECAT_SEARCH` | `ddg` (default, no key), `brave`, `searxng`, or `none` |
| `DECAT_SEARCH_KEY` | only for `brave` |
| `DECAT_SEARXNG_URL` | only for `searxng` — your own instance is the most private option |
| `DECAT_HOME` | where the switch's state and the `Public` history live (default `~/.decat`) |

`DECAT_SEARCH` is the only setting that decides whether anything at all leaves
this machine. The model is local, so with `none` the answer comes from the model's
own knowledge — and the card says so rather than pretending to have sources.

## What is written down

* **`Private`** (the default): **nothing, anywhere.** The question lives in the
  bot's memory from the moment it is asked until the answer is delivered, and then
  it is gone — no row, no file, no log line, not even a counter. The answer is an
  ephemeral Discord reply.
* **`Public`**: the same, plus one line in `~/.decat/history/YYYY-MM.jsonl` on
  this machine (mode `0600`) holding the question, the answer, the sources, the
  model and the time. Nothing is uploaded anywhere.
* **The log** (`~/.decat/agent.log`) holds counts and timings — `id`, `mode`,
  `lane`, `ms`, how many sources, how many characters — and never a word of a
  question or an answer. Search for it yourself: `grep -c question_chars`.
* **Attached pictures** are fetched into memory and handed to the model as they
  are. They are never written to this machine's disk.

## When it is not working

`decat doctor` is the whole answer to this section, and it runs the checks in the
order they fail:

| What it says | What it means |
|---|---|
| `ollama ❌ not reachable` | `ollama serve` is not running (the installer starts it at login) |
| `model ❌ … is not pulled` | `ollama pull qwen3-vl:8b` |
| `credential ❌` | `DECAT_TOKEN` is empty and there is no `ADMIN_PASSKEY` on the host side |
| `host ❌` | the Space is asleep or the URL is wrong; open it in a browser |
| `search ❌` | the engine is blocked on this network — `DECAT_SEARCH=none` still answers |
| `vision ❌` | the model is not a vision model; pull `qwen3-vl` |

From Discord, **`/bot decat`** says what the *host* thinks: whether a machine has
ever polled, how long ago, whether its switch is off, how deep the queue is, how
many answers were served, refused and expired, and which model answered last.

The three things `/decat` can say instead of an answer, and what each one means:

| The card says | What happened |
|---|---|
| *sry, offline rn — can't really msg bbg 😿* | no poll for 40 seconds: this Mac is asleep, shut, or the agent is stopped |
| *the Mac's switched off rn* | the agent is running and the switch says off |
| *the Mac went quiet mid-question* | it took the question and did not finish within three minutes |

## Why it works this way

**The Mac polls; nothing dials in.** No port is open on this laptop, no tunnel URL
goes stale, and sleeping and waking need no reconnection — the poll simply starts
again. The cost is that the host holds a request open for up to fifteen seconds at
a time, which is a few thousand idle requests a day and nothing more.

**Nothing is installed beyond Ollama.** Every network call in this half is
`urllib`, every page is read with `html.parser`, and `decat/mac/requirements.txt`
is deliberately empty.

**A page is only fetched from a host a search returned.** That is the guard
against a page that contains instructions: the model never asks for a URL, the
extractor only follows the results, and the text handed to the model is labelled
as data rather than as instructions.

**The verification pass is why "checked" means something.** Every grounded answer
is drafted once and then re-read against its sources, with the instruction to
delete what the sources do not support. When that pass fails — an empty reply, a
refusal, something too short to be an answer — the draft is kept and the card
stops claiming it was checked.
