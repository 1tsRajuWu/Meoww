"""Entrypoint for Hugging Face Spaces.

Runs the Discord bot in a background thread and serves the Flask API gateway
via high-performance ASGI (a2wsgi + uvicorn) on port 7860.
"""
import os
import sys
import threading
import asyncio
import time
from dotenv import load_dotenv

load_dotenv()

# 1. Prevent keep_alive from binding a separate port; uvicorn serves keep_alive.app on 7860
import keep_alive
keep_alive.run = lambda: None

# 2. Start Discord Bot in a dedicated background thread
import bot

def _run_discord_bot():
    print("[HF-SPACES] Starting Meow Discord Bot in background thread...", flush=True)
    try:
        asyncio.run(bot.main())
    except Exception as exc:
        print(f"[HF-SPACES] ❌ Bot crashed: {exc}", flush=True)

bot_thread = threading.Thread(target=_run_discord_bot, daemon=True)
bot_thread.start()

# 3. Wrap Flask application into ASGI
from a2wsgi import WSGIMiddleware
import uvicorn

app = WSGIMiddleware(keep_alive.app)

# Monkeypatch Hugging Face Spaces LogTee missing isatty for uvicorn
if not hasattr(sys.stdout, "isatty"):
    sys.stdout.isatty = lambda: False
if not hasattr(sys.stderr, "isatty"):
    sys.stderr.isatty = lambda: False

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("GRADIO_SERVER_PORT", 7860)))
    print(f"[HF-SPACES] Serving Meow API and Gateway on port {port}...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None)
