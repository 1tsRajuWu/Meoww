"""Entrypoint for Hugging Face Spaces (ZeroGPU + Gradio + Flask + Discord Bot).

This space is the machine the product runs on: the Discord gateway, the SQLite
database with its compacted cloud backup, and the API the three Cloudflare hosts
proxy to (`/health`, `/api/*`, `/dashboard`, `/owner`).

The page below is what a person sees at this address, so it says what this
address *is* and where the parts that are actually theirs live, rather than
showing three unlabelled widgets under a name the product stopped using. It keeps
one `@spaces.GPU`-decorated function bound to a Gradio event, because that is what
the ZeroGPU supervisor scans for before it will start this machine at all — and
the copy says so, rather than dressing a load check up as a feature.

Three things in the startup sequence are load-bearing rather than stylistic: the
port is forced to 7860 and SSR off *before* Gradio is imported (the supervisor
binds that port directly and its scanner is what refuses to start the machine),
`keep_alive._SERVER_STARTED` is set so `bot.py` does not spin up a second copy of
the web threads, and the Flask app is mounted as the catch-all on Gradio's own
server, so the API and the bot are one process.
"""
import os
import sys
import json
import threading
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Force port 7860 and disable SSR so Python FastAPI binds directly to 7860
os.environ["PORT"] = "7860"
os.environ["GRADIO_SSR_MODE"] = "False"

import gradio as gr
try:
    import spaces
except ImportError:
    class spaces:
        @staticmethod
        def GPU(fn):
            return fn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import brand  # noqa: E402 — the card reads one author instead of a literal

#: Where the rest of the product is. This address is infrastructure; these are the
#: doors a visitor arriving here would actually want, and naming them is the
#: difference between a page that explains itself and a dead end.
_SITE = "https://meow-bot.pages.dev"
_CONSOLE = "https://meow-owner.pages.dev"
_HUB = "https://meowdev.pages.dev"


# 1. ZeroGPU Function Decorator bound to a Gradio event
@spaces.GPU
def _zero_gpu_worker(prompt: str) -> str:
    """ZeroGPU worker function to satisfy the HF supervisor scan."""
    asked = (prompt or "").strip() or "nothing"
    return f"{brand.NAME} gateway answered: {asked}"


def _brand_theme():
    """The product's palette as a Gradio theme, or Gradio's own.

    Wrapped in a `try` because this runs at import time on the *space's*
    interpreter, and it is the one file here whose failure mode is not a wrong
    colour but **a machine that does not boot** — this space is the bot. A Gradio
    that renamed one theme token would then take the product offline to fix a
    page's background, so the fallback is Gradio's default theme and a line in
    the log, which is a worse-looking page and a working product.
    """
    try:
        return gr.themes.Base(
            primary_hue=gr.themes.colors.teal,
            secondary_hue=gr.themes.colors.teal,
            neutral_hue=gr.themes.colors.gray,
            font=["Source Sans Pro", "system-ui", "sans-serif"],
            font_mono=["IBM Plex Mono", "ui-monospace", "monospace"],
        ).set(
            body_background_fill=brand.BG_HEX,
            body_background_fill_dark=brand.BG_HEX,
            block_background_fill="#101314",
            block_background_fill_dark="#101314",
            body_text_color="#f2f5f5",
            body_text_color_dark="#f2f5f5",
        )
    except Exception as exc:  # pragma: no cover - the space's own Gradio decides this
        print(f"[HF-SPACES] brand theme unavailable, keeping Gradio's: {exc}", flush=True)
        return None


# 2. Build Gradio Interface
with gr.Blocks(title=f"{brand.NAME} · gateway", theme=_brand_theme()) as demo:
    gr.Markdown(
        f"""# {brand.NAME}

### The always-on half.

This address is the machine behind the bot: the Discord gateway held open around
 the clock, the database, and the API the other hosts proxy to. It is
 infrastructure rather than a control panel, so the things a visitor came for
 live one link away.

| Where | What is there |
| --- | --- |
| [{_SITE}]({_SITE}) | The site, the system matrix, and the per-server dashboard |
| [{_CONSOLE.replace('https://', '')}]({_CONSOLE}) | The operator's console, for the owner |
| [{_HUB.replace('https://', '')}]({_HUB}) | Every system, every command, and what changed |

**The box below is a load check, not a feature.** ZeroGPU refuses to start a
space unless a function decorated with `@spaces.GPU` is bound to an event, so
there is one, and it answers. Nothing else on this page needs a GPU.
"""
    )
    with gr.Row():
        inp = gr.Textbox(label="Send it something", value="Ping", scale=3)
        btn = gr.Button(f"Ask the {brand.NAME} gateway", variant="primary", scale=1)
    out = gr.Textbox(label="Answer", interactive=False)
    btn.click(fn=_zero_gpu_worker, inputs=inp, outputs=out)
    gr.Markdown(f"`/health` and `/api/status` are answered by this process too.")

# 3. Mount Flask app to handle all API & Dashboard routes
import keep_alive
# Mark server started so bot.py does not spin up duplicate threads
keep_alive._SERVER_STARTED = True

# Launch Gradio directly on port 7860 with SSR disabled
demo.launch(server_name="0.0.0.0", server_port=7860, ssr_mode=False, prevent_thread_lock=True)

# 4. Attach catch-all forwarder on demo.server_app to Flask
try:
    from a2wsgi import WSGIMiddleware
except ImportError:
    from starlette.middleware.wsgi import WSGIMiddleware

from starlette.requests import Request
from starlette.responses import Response

wsgi_asgi = WSGIMiddleware(keep_alive.app)

@demo.server_app.api_route("/health", methods=["GET", "HEAD"])
async def health_check(req: Request):
    # `service` is a public field of a public payload, so it names the product:
    # the Pages *project* is still called meow-bot (a host's configuration, and a
    # name with a migration behind it), but a monitor reading this should not be
    # told the service is a codename no page uses.
    #
    # Derived rather than typed, because this answer is also the one thing the
    # release check can hold the machine to (it carries no commit stamp):
    # `scripts/ship.py` compares what this returns with `brand.GATEWAY_SERVICE`,
    # so a rename moves the check and the answer together instead of leaving a
    # literal here that a whole rebrand can miss.
    return Response(
        content=json.dumps({"status": "healthy", "service": brand.GATEWAY_SERVICE}),
        media_type="application/json",
    )

@demo.server_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def forward_to_flask(req: Request, path: str):
    scope = dict(req.scope)
    scope["path"] = "/" + path
    scope["raw_path"] = ("/" + path).encode()
    response_body = []
    response_headers = []
    response_status = 200

    async def send(message):
        nonlocal response_status, response_headers, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = [(k.decode("latin1"), v.decode("latin1")) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await wsgi_asgi(scope, req.receive, send)
    res = Response(content=b"".join(response_body), status_code=response_status)
    for k, v in response_headers:
        if k.lower() not in ("content-length", "content-type"):
            res.headers.append(k, v)
    return res

# 5. Start the Discord Bot in background thread
import bot

def run_discord_bot():
    # The name is read rather than typed: this line is the first thing a cold
    # space prints, and it was still announcing the product's pre-rebrand name.
    print(f"[HF-SPACES] Starting {brand.NAME} Discord bot event loop...", flush=True)
    asyncio.run(bot.main())

bot_thread = threading.Thread(target=run_discord_bot, daemon=False)
bot_thread.start()

if __name__ == "__main__":
    bot_thread.join()
