"""Set the bot's avatar image. (Newbie helper — this *does* change the bot.)

Usage (run from the repo root):
    python scripts/update_avatar.py [optional/path/to/image.png]

Needs ``DISCORD_TOKEN`` in ``.env``. See ``scripts/README.md``.
"""
import os
import sys

import discord
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

#: Repo root (this file lives in ``scripts/``, so go one level up).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _image_path() -> str:
    """Avatar image: argv[1], else AVATAR_PATH, else the repo's assets folder."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.getenv(
        "AVATAR_PATH",
        os.path.join(ROOT, "assets", "avatar.png"),
    )


class AvatarUpdater(discord.Client):
    async def on_ready(self):
        print("Logged in, updating avatar...")
        path = _image_path()
        try:
            with open(path, "rb") as f:
                avatar_bytes = f.read()
        except OSError as e:
            print(f"Error reading {path}: {e}")
            await self.close()
            return
        try:
            await self.user.edit(avatar=avatar_bytes)
            print("Avatar updated successfully!")
        except Exception as e:
            print(f"Error updating avatar: {e}")

        await self.close()


def main() -> int:
    if not TOKEN:
        print("No DISCORD_TOKEN found (checked the environment and .env).")
        return 1
    intents = discord.Intents.default()
    client = AvatarUpdater(intents=intents)
    client.run(TOKEN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
