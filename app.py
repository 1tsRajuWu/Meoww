"""Entrypoint for Hugging Face Spaces.

Runs the Discord bot in a background process and exposes the Flask web service
alongside a Gradio status interface on port 7860.
"""
import os
import sys
import subprocess
import time
import uvicorn
from fastapi import FastAPI
from starlette.middleware.wsgi import WSGIMiddleware
import gradio as gr
from dotenv import load_dotenv

load_dotenv()

# 1. Start the Discord bot process in background
print("[HF-SPACES] Starting Meow Discord Bot process...", flush=True)
bot_process = subprocess.Popen([sys.executable, "bot.py"])

# 2. Import Flask application from keep_alive
import keep_alive

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
        health_btn = gr.Button("Check Health")
        health_out = gr.Textbox(label="Health Status", value="Checking...")
        
        def check():
            return f"Healthy at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
        
        health_btn.click(check, outputs=health_out)

# 4. Create FastAPI ASGI application
fastapi_app = FastAPI()

# Mount Gradio app on /gradio
fastapi_app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

# Mount Flask app as root to handle /api/*, /dashboard, /owner, /health, etc.
fastapi_app.mount("/", WSGIMiddleware(keep_alive.app))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"[HF-SPACES] Serving API and Dashboard on port {port}...", flush=True)
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
