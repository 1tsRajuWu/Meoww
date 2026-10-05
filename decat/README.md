# 🛰️ Decat — the half that runs on your Mac

`/decat` takes a question in Discord, relays it to **this computer**, answers it
with a model running here, and posts the answer back with the pages it read.
Nothing about a question is sent to a model vendor, because the model is not a
service — it is a program on this machine.

```
Discord ──/decat──► the bot's host ──poll──► this Mac
                     (keeps nothing)          ├─ Ollama: a local model (3.3–6.1 GB)
                                              ├─ search + read pages
                                              ├─ answer, then check it
                                              └─ answer back ──► the Discord card
```

## Install it once

```bash
bash decat/mac/install.sh
```

It installs Ollama (Homebrew), **chooses a model from this machine's memory**,
pulls it (once), writes `~/.decat/agent.env`, installs the `decat` command, and
registers a login item so it starts at login and restarts if it stops. Then it
runs `decat doctor` and tells you what is still missing.

**The model it picks is the one the machine can hold.** `qwen3-vl:8b` needs about
6.1 GB resident and `qwen3-vl:4b` about 3.3 GB, and both read the same pages and
write the same cited answer — so 24 GB of memory or more gets the big one, and a
16 GB laptop gets the small one *and does not download the big one*, because a
6 GB model nobody will load is a thing a machine in a hurry reaches for later.
This is not a guess about your machine: it is what the Air this was written for
measured — with the 8b resident on 16 GB, one answer paged ~1.9 GB out to disk and
took thirty-one seconds. `DECAT_MODEL` overrides the choice; whatever you name has
to be pulled first (`ollama pull qwen3-vl:8b`).

Two things must match before a question can be answered:

1. **The credential, on both sides.** The host and this machine have to present
   the same secret, and the host already has one: it falls back to `ADMIN_PASSKEY`
   while its own `DECAT_TOKEN` is unset. So the quickest working setup is to put
   the *host's* passkey here:

   ```bash
   echo 'DECAT_TOKEN=the-value-of-ADMIN_PASSKEY-on-the-host' >> ~/.decat/agent.env
   ```

   **A dedicated token is better**, and worth the two minutes: the passkey opens
   the operator's entire dashboard, and a laptop is the machine most likely to be
   lost. Generate one (`openssl rand -hex 32`), set it as `DECAT_TOKEN` on the
   host — the Space's *Settings → Variables and secrets* page (as a **secret**, so
   it is stored write-only), or
   `POST /api/spaces/1tsRajOwO/meow/secrets` with `{"key": "DECAT_TOKEN",
   "value": "…"}` on the Hub — and put the same value here. Leaving it blank on *this* side only works if this machine also has
   `ADMIN_PASSKEY` in its environment, which the installer does not write. From
   Discord, **`/bot decat` says which of the two the host is using**, so the
   weaker choice is visible rather than assumed.
2. **The server must have the system switched on** — an administrator, once.
   `/bot decat` answers this too. The two servers the deployment names in
   `DECAT_GUILDS` both have it on; anywhere else the command is not offered at all.

## Asking for a deep answer or a fast one

`/decat` takes a **depth**, and you pick it:

* **Deep** (the default) — a wider read (three pages, two searches) and the second
  pass that re-reads the draft against those pages and deletes what they do not
  support. Slower, and the answer says *"checked against the sources it cites"*.
* **Fast** — one search, two pages, one pass, no re-read. It says *"drafted from
  the sources it cites — the quick answer, not re-read"*, so it never borrows a
  claim it did not earn.

Nothing about the choosing is hidden: each card states which of the two it was and
which model wrote it. **The two depths are not two models** — on a machine that
took the small model, Deep and Fast are the same weights, and the difference is
entirely the reading and the re-check, which is most of what the wait is. Set
`DECAT_FAST_MODEL` if you want Fast to answer with a *different* model as well;
blank it and Fast is still quicker.

## Turning it on and off

Three switches, and they answer three different questions. This is the whole
answer to "how do I turn it off":

* **This machine.** `decat off` / `decat on` / `decat status` in a terminal, or the
  one button at **`http://127.0.0.1:8787`** — that page also shows the facts you
  actually want (whether Ollama is up, whether the model is pulled, whether the
  host answers, whether search does). Bound to loopback, so nothing on your
  network can reach it and it holds no credential. This is the switch that lives
  on the Mac, and it is the one the asker hears about.
* **One server.** The **Decat** switch on that server's dashboard Systems page
  (an administrator). Off means `/decat` is not offered in that server at all — the
  command leaves its picker rather than refusing when somebody clicks it.
* **The deployment.** `DECAT_GUILDS` on the host is the hard allow-list of servers
  `/decat` may ever run in; blank means the per-server switch decides. Currently
  the two servers named in it.

While the Mac's switch says **off** the machine keeps polling and keeps saying
"off", so the bot answers *"the Mac's switched off rn"* instead of *"offline"* — a
switch is only worth having if the difference is visible from Discord. And
**`/bot decat`** puts all of it in one card from Discord: whether a machine has
polled, how long ago, what its switch says, how deep the queue is, and how the
last answer went.

## `~/.decat/agent.env`

| Setting | Meaning |
|---|---|
| `DECAT_TOKEN` | the credential the host expects (see above) |
| `DECAT_SPACE` | where the bot lives |
| `DECAT_MODEL` | the model. Chosen by the installer from this machine's memory — `qwen3-vl:4b` (3.3 GB) under 24 GB, `qwen3-vl:8b` (6.1 GB) at or above it. Vision, tool-capable, Apache-2.0 |
| `DECAT_FAST_MODEL` | the model a **Fast** question answers with, when it should differ. Blank means "the same as the one above", and Fast still reads fewer pages and skips the re-read |
| `DECAT_KEEP_ALIVE` | how long the weights stay in memory after an answer — `10m` by default. `0` gives the RAM back at once and the next question pays the load again; `-1` holds them until Ollama stops |
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
| `model ❌ … is not pulled` | `ollama pull <the model agent.env names>` |
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
