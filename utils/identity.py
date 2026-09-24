"""The bot's name and picture **in one server** — a thing Discord really supports.

Discord lets a bot carry a per-server profile, and it is not a cosmetic trick
with a shared backend: ``PATCH /guilds/{guild_id}/members/@me`` accepts ``nick``,
``avatar``, ``banner`` and ``bio``, and the fields are the *bot's own member* in
that one guild. So a server can put its own name and icon on the bot without
touching what every other server sees, and without touching the application's
global avatar (which stays the operator's, the way ``/bot avatar`` leaves it).

Two things this module deliberately does **not** do:

* It never edits the application profile (``bot.user.edit``). That is the global
  one, one call the wrong way round would rename the bot everywhere, and it stays
  behind ``/bot avatar`` and ``/bot banner`` where it belongs.
* It never guesses that a change is wanted. ``KEEP`` means "leave this field
  alone" — the difference between clearing a nickname and not mentioning it is
  the difference between a server asking for a change and a form that re-sends
  everything it knows.

The images are stored here as well as applied, because Discord is the authority
on what is *live* and this is the authority on what should be: a dashboard that
showed an empty avatar the moment it stopped holding the upload would look like
the bot had lost its picture.
"""
from __future__ import annotations

import io

from PIL import Image

from utils import db

#: Leave this field as it is. Distinct from ``None``, which clears it.
KEEP = object()

#: Discord's own ceiling for an uploaded avatar, kept well under it so a phone
#: photo does not become a 10 MB base64 body.
MAX_IMAGE_BYTES = 4 * 1024 * 1024

#: The longest edge a stored avatar is kept at. Discord draws a server's icon far
#: smaller than this, and the record is only ever shown as a small circle on the
#: dashboard beside the form that set it.
AVATAR_MAX_EDGE = 512

#: The banner is the wide one, so it keeps more of its width — but not the
#: multi-megabyte original.
BANNER_MAX_EDGE = 1024

#: Pictures at or under this, already inside the edge, are stored exactly as
#: they arrived: re-encoding a file that is already a thumbnail costs quality and
#: saves bytes nobody was worried about.
ALREADY_SMALL_BYTES = 48 * 1024

#: What a re-encode is written at. High enough that nobody can tell it was
#: re-encoded, low enough to be a fraction of the original.
JPEG_QUALITY = 85

#: What a server may call the bot. Discord truncates a nickname past this.
MAX_NICK = 32

#: Discord's limit for a bot's per-server bio.
MAX_BIO = 190

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
)

EMPTY_PROFILE: dict = {
    "nick": None, "bio": None, "avatar": None, "banner": None,
    "updated_at": None, "updated_by": None,
}


def image_problem(data: bytes | None) -> str | None:
    """Why these bytes are not an image Discord will accept, or ``None``.

    Checked here so the answer is a sentence rather than a 400 from Discord:
    ``avatar`` and ``banner`` are sent as base64 and Discord answers a bare
    "Invalid Form Body" for anything it cannot decode, which tells an
    administrator nothing about which box to fix.
    """
    if not data:
        return "That file arrived empty — pick the image again."
    if len(data) > MAX_IMAGE_BYTES:
        return (
            f"That image is {len(data) / 1024 / 1024:.1f} MB; the limit here is "
            f"{MAX_IMAGE_BYTES // 1024 // 1024} MB."
        )
    if any(data.startswith(magic) for magic, _ in _MAGIC):
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return None
    return "That is not a PNG, JPEG, GIF or WEBP image."


def tame_image(data: bytes | None, *, max_edge: int) -> bytes | None:
    """A stored picture, shrunk to something worth uploading over and over.

    Two copies of these bytes leave this process on every save and are counted
    twice on the invoice: one to Discord, as base64 inside the member edit, and
    one to Supabase, inside the ``bot.db`` that is uploaded after every change
    and snapshotted hourly. A phone photograph inside that file is also the part
    of it that *cannot* be compressed — a JPEG is already compressed — so an
    unbounded picture makes every future backup bigger for good.

    So the picture is re-encoded once, here, and both copies are the small one.
    Three things it refuses to do, because a picture this host cannot improve is
    still a picture the server asked for:

    * **Bytes that are not a picture it can read** are returned untouched. The
      check above is a magic-byte check, so a file that *says* PNG and decodes
      to nothing reaches here, and guessing at it would store nothing at all.
    * **An animation** is left alone. Flattening the first frame of a GIF would
      silently delete the thing that was uploaded.
    * **A re-encode that did not come out smaller** is discarded. The original
      is the answer when shrinking is not on offer.
    """
    if not data:
        return data
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
            if getattr(picture, "n_frames", 1) > 1:
                return data
            width, height = picture.size
            if max(width, height) <= max_edge and len(data) <= ALREADY_SMALL_BYTES:
                return data
            keeps_alpha = picture.mode in ("RGBA", "LA") or (
                picture.mode == "P" and "transparency" in picture.info
            )
            if max(width, height) > max_edge:
                picture.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if keeps_alpha:
                picture.save(out, format="PNG", optimize=True)
            else:
                picture.convert("RGB").save(
                    out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True
                )
            packed = out.getvalue()
    except Exception:
        return data
    return packed if len(packed) < len(data) else data


def name_problem(nick: str | None) -> str | None:
    """Why a server may not call the bot that, or ``None``."""
    if nick is None:
        return None
    if len(nick) > MAX_NICK:
        return f"That name is {len(nick)} characters; Discord allows {MAX_NICK}."
    return None


def bio_problem(bio: str | None) -> str | None:
    if bio is None:
        return None
    if len(bio) > MAX_BIO:
        return f"That bio is {len(bio)} characters; Discord allows {MAX_BIO}."
    return None


def read(guild_id: int) -> dict:
    """This server's stored profile — an empty one when it has never been set.

    Synchronous, like ``db.read_guild_modules_sync``: the Flask thread and the
    bot's loop both call it, and one access pattern is one place to be wrong.
    """
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nick, bio, avatar, banner, updated_at, updated_by "
            "FROM guild_identity WHERE guild_id = ?",
            (guild_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return dict(EMPTY_PROFILE)
    return {
        "nick": row[0], "bio": row[1], "avatar": row[2], "banner": row[3],
        "updated_at": row[4], "updated_by": row[5],
    }


def write(
    guild_id: int,
    *,
    nick: str | None,
    bio: str | None,
    avatar=KEEP,
    banner=KEEP,
    updated_by: int | None = None,
) -> bool:
    """Persist a server's profile. ``KEEP`` leaves a stored image alone.

    Returns ``False`` when the write failed, so a caller can report a failure
    rather than claiming a change that would vanish on the next read.

    Both pictures pass :func:`tame_image` on the way in. This is the last door
    before the file every backup carries, and a bound that only the HTTP route
    remembers to apply is not a bound: the same helper is called there, so this
    second pass is a no-op for a picture that was already shrunk.
    """
    if avatar is not KEEP and avatar is not None:
        avatar = tame_image(avatar, max_edge=AVATAR_MAX_EDGE)
    if banner is not KEEP and banner is not None:
        banner = tame_image(banner, max_edge=BANNER_MAX_EDGE)
    if avatar is KEEP or banner is KEEP:
        current = read(guild_id)
        if avatar is KEEP:
            avatar = current["avatar"]
        if banner is KEEP:
            banner = current["banner"]
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO guild_identity
                   (guild_id, nick, bio, avatar, banner, updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   nick = excluded.nick,
                   bio = excluded.bio,
                   avatar = excluded.avatar,
                   banner = excluded.banner,
                   updated_at = CURRENT_TIMESTAMP,
                   updated_by = excluded.updated_by""",
            (guild_id, nick, bio, avatar, banner, updated_by),
        )
        conn.commit()
        return True
    except Exception as exc:
        print(f"[IDENTITY] ❌ Could not save the profile for {guild_id}: {exc}", flush=True)
        return False
    finally:
        conn.close()


def clear(guild_id: int) -> bool:
    """Forget this server's profile. The caller resets Discord to match."""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM guild_identity WHERE guild_id = ?", (guild_id,))
        conn.commit()
        return True
    except Exception as exc:
        print(f"[IDENTITY] ❌ Could not clear the profile for {guild_id}: {exc}", flush=True)
        return False
    finally:
        conn.close()


async def apply(member, *, nick=KEEP, bio=KEEP, avatar=KEEP, banner=KEEP) -> None:
    """Push changes onto the bot's own member in one server.

    ``KEEP`` fields are not mentioned at all, so a form that only changes the
    nickname cannot clear the avatar as a side effect. ``None`` clears a field,
    and an empty nickname is how Discord spells "no nickname in this server".
    """
    kwargs: dict = {}
    if nick is not KEEP:
        kwargs["nick"] = nick or ""
    if bio is not KEEP:
        kwargs["bio"] = bio or ""
    if avatar is not KEEP:
        kwargs["avatar"] = avatar
    if banner is not KEEP:
        kwargs["banner"] = banner
    if not kwargs or member is None:
        return
    await member.edit(**kwargs)
