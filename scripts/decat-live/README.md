# decat.pages.dev deployment bundle (checked in so any redeploy is identical).

`decat.pages.dev` is a static shell plus this edge worker: every page route
is reverse-proxied to the Flask backend on Hugging Face
(`1tsrajowo-meow.hf.space`), which is the machine that actually runs the bot.
Only `/static/*` is served from the CDN. The backend serves its Gradio shell
at `/`, so the front door reads through to `/systems` — same screen the old
deployment served at every path, now live instead of frozen.

Redeploy (needs a Pages upload token for the `decat` project):

    npx wrangler pages deploy scripts/decat-live \
      --project-name=decat --branch=main --commit-hash=$(git rev-parse HEAD)

`favicon.svg` is served from the repo's own `static/` (copied here at deploy
time); the worker file and route list are the whole deployment besides it.
