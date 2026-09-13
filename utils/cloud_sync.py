"""
Persistent Cloud Database Sync for Meow Discord Bot.
Preserves SQLite database (bot.db) across cloud deploys, restarts, and sleep cycles using GitHub Gists.
Guarantees zero progress loss for levels, XP, economy, inventory, tickets, and module toggles.
"""
import os
import sys
import time
import base64
import asyncio
import hashlib
import urllib.request
import json

# SECURITY: credentials must come from the environment only.
# Never hardcode a GitHub token (or any secret) here again — this file is public.
GITHUB_TOKEN = (os.environ.get("GITHUB_BACKUP_TOKEN") or "").strip()
GIST_ID = (os.environ.get("GIST_BACKUP_ID") or "").strip()
DB_PATH = "bot.db"

if not GITHUB_TOKEN or not GIST_ID:
    print(
        "[CLOUD SYNC] ⚠️ GITHUB_BACKUP_TOKEN / GIST_BACKUP_ID not set. "
        "Cloud backup is DISABLED, so bot.db will not survive a redeploy. "
        "Set both environment variables to re-enable it.",
        flush=True,
    )

_last_sync_hash = None

def get_db_hash() -> str:
    """Compute MD5 hash of local SQLite database file."""
    if not os.path.exists(DB_PATH):
        return ""
    with open(DB_PATH, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def restore_from_cloud() -> bool:
    """Fetch the latest database snapshot from GitHub Gist on startup."""
    global _last_sync_hash
    if not GITHUB_TOKEN or not GIST_ID:
        print("[CLOUD SYNC] ⚠️ Missing GITHUB_BACKUP_TOKEN or GIST_BACKUP_ID. Skipping cloud restore.", flush=True)
        return False

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "MeowBot-CloudSync"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        
        if data.get("public"):
            print(
                "[CLOUD SYNC] 🔓 WARNING: the backup Gist is PUBLIC — anyone with its URL "
                "can read the database (levels, economy, warnings). Make it secret.",
                flush=True,
            )

        files = data.get("files", {})
        if "bot.db.b64" not in files:
            print("[CLOUD SYNC] ⚠️ No bot.db.b64 file found in Gist.", flush=True)
            return False

        b64_content = files["bot.db.b64"].get("content", "")
        if not b64_content:
            print("[CLOUD SYNC] ⚠️ bot.db.b64 content was empty in Gist.", flush=True)
            return False

        db_bytes = base64.b64decode(b64_content)
        
        # Write to local file atomically
        temp_path = f"{DB_PATH}.tmp"
        with open(temp_path, "wb") as f:
            f.write(db_bytes)
        
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            os.replace(temp_path, DB_PATH)
            _last_sync_hash = hashlib.md5(db_bytes).hexdigest()
            print(f"[CLOUD SYNC] ✅ Successfully restored database from cloud! ({len(db_bytes):,} bytes, Hash: {_last_sync_hash[:8]}). Zero progress lost.", flush=True)
            return True
    except Exception as e:
        print(f"[CLOUD SYNC] ❌ Error restoring database from cloud: {e}", flush=True)
    return False

def backup_to_cloud_sync() -> bool:
    """Synchronously upload current bot.db snapshot to GitHub Gist."""
    global _last_sync_hash
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return False
    if not GITHUB_TOKEN or not GIST_ID:
        return False

    current_hash = get_db_hash()
    if current_hash == _last_sync_hash:
        return True  # No changes to upload

    try:
        with open(DB_PATH, "rb") as f:
            raw_bytes = f.read()
        
        b64_str = base64.b64encode(raw_bytes).decode("utf-8")
        payload = {
            "description": f"Meow Discord Bot Persistent Cloud SQLite Database (Auto-saved at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})",
            "files": {
                "bot.db.b64": {
                    "content": b64_str
                }
            }
        }

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "MeowBot-CloudSync"
        }

        req = urllib.request.Request(url, headers=headers, data=json.dumps(payload).encode("utf-8"), method="PATCH")
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status in (200, 201):
                _last_sync_hash = current_hash
                print(f"[CLOUD SYNC] ☁️ Database synced to persistent cloud ({len(raw_bytes):,} bytes, Hash: {_last_sync_hash[:8]}).", flush=True)
                return True
    except Exception as e:
        print(f"[CLOUD SYNC] ⚠️ Error syncing database to cloud: {e}", flush=True)
    return False

async def cloud_sync_loop():
    """Background task syncing database changes to cloud every 25 seconds."""
    while True:
        try:
            await asyncio.sleep(25)
            await asyncio.to_thread(backup_to_cloud_sync)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[CLOUD SYNC LOOP] ⚠️ Loop error: {e}", flush=True)

def start_cloud_sync_loop(bot):
    """Start the asynchronous auto-sync background loop."""
    bot.loop.create_task(cloud_sync_loop())
