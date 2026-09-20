# The operator console

This is the private management app for the bot. It is served by Flask at
**`/owner`** and it is reachable **only by an operator sign-in** (the admin
passkey, or a one-time link minted by the operator) — every page and every
endpoint behind it refuses anything else, including a one-time link that belongs
to a single server.

It replaced four server-rendered pages that had grown into a scroll, and the
public read-only mirror that used to sit on `meowdev.pages.dev`. That mirror
could never have been a management tool: it was a static host with no session,
so nothing it drew could change anything.

**`meowdev.pages.dev` now forwards here** (301, path preserved) rather than
serving a copy of anything. It was published once by hand and never wired to the
repository, so every push updated `meow-bot.pages.dev` and left that address
showing a frozen build — which is the whole reason it looked unchanged after an
update. One address, one site, and it cannot go stale. Its hub pages are still
buildable with `scripts/build_hub.py` if it is ever wanted back.

## Where things are

| | |
|---|---|
| `src/App.tsx` | reads the shell's boot data, fetches the fleet once, routes the pages |
| `src/api.ts` | **every** request goes through here; it attaches the CSRF token |
| `src/lib/router.tsx` | ~60 lines of history routing, instead of a dependency |
| `src/lib/useLive.ts` | the polling hook — the one place an interval is set |
| `src/lib/theme.ts` | the four themes' names; the colours are in `styles.css` |
| `src/ui.tsx` | the building blocks: card, switch, toast, skeleton, empty |
| `src/pages/*` | one file per page, each fetching what it draws |
| `src/styles.css` | tokens, then layout, then components, in that order |

The server side is `keep_alive.py`: the shell route (`owner_console`) and five
`/api/console/*` routes that open with `_require_operator_api`. The API reuses
the same helpers the old console used, so the app cannot disagree with the
dashboard about a server's state.

## Building it

```bash
cd console
npm ci
npm run build      # typechecks, bundles, then stamps the result
```

The build writes `static/console/` — `app.js`, `app.css` and `build.json`.

**`static/console/` is committed**, which this repository does not do with
generated output anywhere else (`_site/` and `_hub/` are both ignored). The
reason is the host: the console is served by a Python runtime with no guaranteed
Node, and a deploy that had to build this could fail to build it. What normally
goes wrong with committing generated output is that it goes stale quietly, so
that is closed instead of accepted — `build.json` holds a hash of everything
under `src/` plus the config, and `tests/test_console.py` recomputes it. Edit a
source file and forget to rebuild, and the suite fails with the command to run.

So: **`npm run build` before you commit any change under `console/`.**

## Working on it

```bash
npm run dev        # Vite dev server, fast reload; needs a signed-in Flask behind it
npm run typecheck  # tsc --noEmit, no bundle
```

For a visual pass with real data, `scripts/preview_dashboard.py` serves this
shell at `/owner` and mounts the real `/api/console/*` views against its own
database — the preview signs itself in, because the harness has no sign-in and
the API behind it is operator-only.

## Two rules worth keeping

- **The server owns anything that can be wrong about a session.** The CSRF token
  and the gateway state are rendered into the page as inert JSON
  (`<script type="application/json" id="console-boot">`), never as an inline
  script: this page's CSP carries `unsafe-inline` for the older templates, and a
  page that only runs code it shipped has no business spending that allowance.
- **A switch reports what the server said, not what it hoped.** The knob moves
  immediately and springs back with the server's own sentence if the save is
  refused — the defect this project has been reported for twice is a toggle that
  reverts with no reason on screen.
