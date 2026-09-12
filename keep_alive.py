"""
Tiny web server so free hosting platforms have something to 'see' as running,
and so uptime-monitor services (like UptimeRobot) can ping the bot to help
prevent it from sleeping on platforms that idle inactive services.
"""
import os
from flask import Flask
from threading import Thread
import socket

app = Flask("")


@app.route("/")
def home():
    return "Bot is alive!"


def find_free_port(start=8080):
    """Find a free port starting from `start`."""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start  # fallback


def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
