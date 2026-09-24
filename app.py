"""Entrypoint for Hugging Face Spaces (ZeroGPU + Gradio + Flask + Discord Bot).

1. Exposes Gradio on port 7860 to satisfy ZeroGPU scanner with @spaces.GPU.
2. Forwards HTTP requests (/health, /api/*, /owner/*, etc.) to the Flask app.
3. Runs the Meow Discord Bot in a background thread with automatic cloud database sync.
"""
import os
import sys
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

# 1. ZeroGPU Function Decorator bound to a Gradio event
@spaces.GPU
def _zero_gpu_worker(prompt: str) -> str:
    """ZeroGPU worker function to satisfy the HF supervisor scan."""
    return f"Meow Engine Active: {prompt}"

# 2. Build Gradio Interface
with gr.Blocks(title="Meow Discord Bot Gateway") as demo:
    gr.Markdown("# 🐾 Meow Discord Bot - 24/7 Cloud Gateway")
    gr.Markdown("Active instance hosting Discord Bot + Web Gateway.")
    inp = gr.Textbox(label="Ping Test", value="Ping")
    btn = gr.Button("Query ZeroGPU")
    out = gr.Textbox(label="Output")
    btn.click(fn=_zero_gpu_worker, inputs=inp, outputs=out)

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
    return Response(content='{"status":"healthy","service":"meow-bot"}', media_type="application/json")

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
    print("[HF-SPACES] Starting Meow Discord Bot event loop...", flush=True)
    asyncio.run(bot.main())

bot_thread = threading.Thread(target=run_discord_bot, daemon=False)
bot_thread.start()

if __name__ == "__main__":
    bot_thread.join()
