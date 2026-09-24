"""Entrypoint for Hugging Face Spaces.

Starts the Flask Web Gateway immediately in a daemon thread so port 7860
passes the host health check within 50ms, and runs the Discord Bot on the
main thread with full signal handling.
"""
import os
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Force port 7860 for Hugging Face Spaces
os.environ["PORT"] = "7860"

# 1. Start Flask web server immediately (<50ms)
import keep_alive
keep_alive.keep_alive()

# 2. Run Discord Bot on the main thread
import bot

if __name__ == "__main__":
    print("[HF-SPACES] Starting Meow Discord Bot on Hugging Face Spaces...", flush=True)
    asyncio.run(bot.main())
