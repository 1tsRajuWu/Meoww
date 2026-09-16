#!/bin/bash
# Dev helper: run the bot locally and restart it if it crashes.
# Usage:  bash scripts/run-dev.sh   (run from anywhere; it finds the repo root)
# Needs:  a real DISCORD_TOKEN in .env (copy .env.example -> .env first).
set -u

# Repo root = parent of this script's folder. No hardcoded paths.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# Use the project venv when it exists, otherwise fall back to system python.
if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

PYTHON_BIN="python3"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN="python"

echo "Starting Bot Daemon... (root: $ROOT)"

while true; do
    if [ -f .env ]; then
        # Check if the .env file has a placeholder token
        TOKEN=$(grep -E "^DISCORD_TOKEN=" .env | cut -d '=' -f2)
        if [ -n "$TOKEN" ] && [ "$TOKEN" != "paste_your_bot_token_here" ] && [ "$TOKEN" != "your_actual_token_here" ]; then
            "$PYTHON_BIN" bot.py
            echo "Bot crashed or exited. Restarting in 5 seconds..."
        else
            echo "Waiting for valid DISCORD_TOKEN in .env..."
        fi
    else
        echo ".env file not found. Copy .env.example to .env first. Waiting..."
    fi
    sleep 5
done
