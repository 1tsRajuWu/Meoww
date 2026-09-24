"""Entrypoint for Hugging Face Spaces.

Runs the Discord bot and Flask ASGI gateway concurrently in the primary
asyncio event loop on port 7860.
"""
import os
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()

# 1. Prevent keep_alive from starting a duplicate background thread
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

async def main():
    port = int(os.environ.get("PORT", os.environ.get("GRADIO_SERVER_PORT", 7860)))
    print(f"[HF-SPACES] Starting Meow Gateway on port {port} and Discord Bot concurrently...", flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None)
    server = uvicorn.Server(config)

    # Run bot and web server concurrently in the main event loop
    await asyncio.gather(
        bot.main(),
        server.serve(),
    )

if __name__ == "__main__":
    asyncio.run(main())
