"""Entrypoint for Hugging Face Spaces.

Starts the ASGI Web Gateway (uvicorn + a2wsgi) immediately in a background
thread so port 7860 satisfies the host health check within 100ms, and runs
the Discord bot on the main thread with full signal handling.
"""
import os
import sys
import threading
import asyncio
import time
from dotenv import load_dotenv

load_dotenv()

# 1. Prevent keep_alive from starting a separate Flask server on port 8080
import keep_alive
keep_alive.run = lambda: None

# 2. Import Discord bot and ASGI middleware
import bot
from a2wsgi import WSGIMiddleware
import uvicorn

# Wrap Flask into ASGI
app = WSGIMiddleware(keep_alive.app)

# Monkeypatch Hugging Face LogTee missing isatty for uvicorn
if not hasattr(sys.stdout, "isatty"):
    sys.stdout.isatty = lambda: False
if not hasattr(sys.stderr, "isatty"):
    sys.stderr.isatty = lambda: False

def _run_server():
    port = int(os.environ.get("PORT", os.environ.get("GRADIO_SERVER_PORT", 7860)))
    print(f"[HF-SPACES] Starting ASGI Web Gateway on port {port}...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None)

# 3. Start web server in background thread IMMEDIATELY (<100ms)
server_thread = threading.Thread(target=_run_server, daemon=True)
server_thread.start()

# 4. Run Discord bot on main thread
if __name__ == "__main__":
    print("[HF-SPACES] Starting Meow Discord Bot on main thread...", flush=True)
    try:
        asyncio.run(bot.main())
    except Exception as exc:
        print(f"[HF-SPACES] ❌ Bot finished with: {exc}", flush=True)
