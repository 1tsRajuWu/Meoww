#!/bin/bash
cd /Users/raj/Documents/discord-bot
source .venv/bin/activate

echo "Starting Bot Daemon..."

while true; do
    if [ -f .env ]; then
        # Check if the .env file has a placeholder token
        TOKEN=$(grep -E "^DISCORD_TOKEN=" .env | cut -d '=' -f2)
        if [ -n "$TOKEN" ] && [ "$TOKEN" != "paste_your_bot_token_here" ] && [ "$TOKEN" != "your_actual_token_here" ]; then
            python bot.py
            echo "Bot crashed or exited. Restarting in 5 seconds..."
        else
            echo "Waiting for valid DISCORD_TOKEN in .env..."
        fi
    else
        echo ".env file not found. Waiting..."
    fi
    sleep 5
done
