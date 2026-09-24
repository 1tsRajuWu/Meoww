"""Entrypoint for Hugging Face Spaces.

Runs the Discord bot in a background thread and serves the Flask API gateway
alongside a Gradio status interface on port 7860.
"""
import os
import sys
import threading
import asyncio
import time
from dotenv import load_dotenv

load_dotenv()

# 1. Start Discord Bot in a dedicated background thread
import bot

def _run_discord_bot():
    print("[HF-SPACES] Starting Meow Discord Bot in background thread...", flush=True)
    try:
        asyncio.run(bot.main())
    except Exception as exc:
        print(f"[HF-SPACES] ❌ Bot crashed: {exc}", flush=True)

bot_thread = threading.Thread(target=_run_discord_bot, daemon=True)
bot_thread.start()

# 2. Import Flask application from keep_alive
import keep_alive
from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
import gradio as gr
import uvicorn

# 3. Create Gradio status interface
with gr.Blocks(title="Meow Discord Bot") as demo:
    gr.Markdown(
        """
        # 🐱 Meow Bot Service
        
        The bot backend and API gateway are running.
        
        * **Status**: 🟢 Online
        * **Platform**: Hugging Face Spaces
        * **Web Dashboard**: Enabled
        * **Voice & Radio**: Enabled
        """
    )
    with gr.Row():
        ping_btn = gr.Button("Ping Bot")
        status_box = gr.Textbox(label="Status", value="Ready")
        
        def ping():
            return f"Healthy & Connected at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
        
        ping_btn.click(ping, outputs=status_box)

# 4. Create FastAPI ASGI application
fastapi_app = FastAPI()

# Mount Gradio app on /gradio
fastapi_app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

# Mount Flask app as root to handle /api/*, /dashboard, /owner, /health, /
fastapi_app.mount("/", WSGIMiddleware(keep_alive.app))

# Expose app at module level for ASGI runners
app = fastapi_app

# Monkeypatch Hugging Face Spaces LogTee missing isatty for uvicorn
if not hasattr(sys.stdout, "isatty"):
    sys.stdout.isatty = lambda: False
if not hasattr(sys.stderr, "isatty"):
    sys.stderr.isatty = lambda: False

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("GRADIO_SERVER_PORT", 7860)))
    print(f"[HF-SPACES] Serving API and Dashboard on port {port}...", flush=True)
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_config=None)


