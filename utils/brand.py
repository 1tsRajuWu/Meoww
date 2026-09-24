"""What this product is called and what it looks like — written down once.

Every surface of this project answers two questions about its own identity: what
is this thing *called*, and what colour is it. Both were answered in more places
than anyone could count, and the failure that produces is never an error — it is
a page whose wordmark says one thing and whose `app.js` fallback says another, or
a palette change that recolours the site and leaves the Discord cards blurple.

The two facts that made this module necessary:

* **The accent lived in two unrelated places.** The web sheets declare it as a
  CSS token (`--accent` in `static/meow.css`) and every card the bot sends reads
  it from `utils/style.ACCENT` as a Discord colour integer. Nothing held the two
  together, so a rebrand was two edits in the hope that somebody made both.
* **The name was already source-dependent.** `keep_alive._bot_display_name()`
  prefers the *running application's* name from Discord and falls back to a
  literal in the middle of a 10,000-line file. That fallback is the brand, and it
  was spelled out where nobody would look for it.

So this module is stdlib-only and dependency-free on purpose — the bot process,
the Flask thread, the two static builders and the test-suite all import it, and a
module they can disagree about is not a single author.

**What deliberately does *not* change with the name.** A rename reaches every
string a person reads and every drawing a person sees. It does not reach the
identifiers that carry *state* or *deployment*: the `MEOW_*` environment
variables (which are answered in a host's dashboard, not in a file this repo
owns), the Cloudflare Pages project names, the `meow_bank` table, the `/meow`
command, the `meow-theme` `localStorage` key and the `window.MeowTheme` global.
Renaming those would be a migration with a data migration's risk and no visible
reward, so they are listed in :data:`INTERNAL_NAMES` and the law in
`tests/test_brand.py` is the *rendered* surfaces, not the code.

The old names stay here as constants rather than as a comment, because two laws
need to look for them: the mark family that was drawn for a retired palette
vocabulary, and the wordmark that must no longer appear on a page.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# The name
# --------------------------------------------------------------------------- #
#: What the product is called. `keep_alive._bot_display_name()` prefers the live
#: Discord application name and falls back to this, so renaming the application
#: in the developer portal is what actually moves every page: this is the answer
#: for a process that has not connected yet, or has no application to read.
NAME = "Keystone"

#: What it is, in one line — the sentence a `og:description` or a footer wants.
TAGLINE = "One bot that runs the server."

#: The letter the header's tile draws before the wordmark. Derived from the name
#: by the law at the bottom of `tests/test_brand.py`, so an initial that no longer
#: matches is a failure rather than a detail.
INITIAL = "K"

#: The name this product used before the rebrand. Nothing a person reads may
#: still say it; the law asserts that over the templates, and the two places that
#: are allowed to mention it are this line and the CHANGELOG's own history.
RETIRED_NAME = "Meow Bot"

# --------------------------------------------------------------------------- #
# The accent
# --------------------------------------------------------------------------- #
#: The value every card the bot sends carries, as Discord wants it: one integer,
#: `0xRRGGBB`. Read by `utils/style.ACCENT` rather than typed again there.
ACCENT = 0x2DD4BF

#: The same colour as the sheets state it. `ACCENT` and this *are* one fact, and
#: the law asserts the sheet still spells it this way.
ACCENT_HEX = "#2dd4bf"

#: `--accent-rgb`, for the glows and rings that need the channels separately.
ACCENT_RGB = "45 212 191"

#: The shade anything *written* in the accent uses. A fill and a line of text are
#: held to different contrast floors (3:1 against 4.5:1), so one value cannot
#: pass both — which is why the sheet has carried two names since long before this
#: rebrand, and why a rebrand has to move both.
ACCENT_TEXT_HEX = "#8bf0e4"
ACCENT_TEXT_RGB = "139 240 228"

#: The page ground. One literal, here, because a page's `<meta name="theme-color">`
#: is served as markup rather than resolved from the sheet — the script is
#: deliberately forbidden from rewriting it (see `tests/test_theme.py`), so the
#: value needs an author that is not "whatever somebody typed into the head".
BG_HEX = "#0a0b0c"
BG_DEEP_HEX = "#050607"

#: The palettes that exist. `dark` is `:root` and has no block of its own, which
#: is why it leads: a visitor who has chosen nothing gets a whole palette.
THEMES: tuple[str, ...] = ("dark", "light", "contrast")

#: The palette the sheet declares on `:root`, token → value. Kept here as data as
#: well as in the sheet because two callers need to *state* it rather than read
#: it: the brand sheet page (`scripts/brand_sheet.py`) and the docs. The sheet
#: remains the author for the browser; the law asserts the two agree.
PALETTE: dict[str, str] = {
    "bg": BG_HEX,
    "bg-deep": BG_DEEP_HEX,
    "surface-card": "#101314",
    "surface-sunken": "#0c0f10",
    "surface-hover": "#171b1c",
    "text-primary": "#f2f5f5",
    "text-secondary": "#c6cece",
    "text-muted": "#8b9797",
    "text-invert": BG_HEX,
    "accent": ACCENT_HEX,
    "accent-text": ACCENT_TEXT_HEX,
    "accent-rgb": ACCENT_RGB,
    "accent-text-rgb": ACCENT_TEXT_RGB,
    "good": "#4ade80",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "line": "#1f2526",
    "line-strong": "#333c3d",
}

# --------------------------------------------------------------------------- #
# The drawings
# --------------------------------------------------------------------------- #
#: **One** mark, because the mark carries its own ground.
#:
#: The generation before this one shipped a mark per theme — four files drawn for
#: a nine-theme menu — and the failure of that arrangement is not hypothetical: a
#: theme asked for `logo-aurora.svg`, no such file existed, and the browser drew
#: its broken-image glyph beside the product's own name with no console error and
#: nothing a test could see. Two marks is two chances to be missing one.
#:
#: So the drawing is a badge: an accent tile with the keystone knocked out of it.
#: It is legible on the house black, on the light palette and on the high-contrast
#: one without knowing which is in use, which is why the mapping below points
#: every palette at the same file. If a future mark *does* need a different
#: drawing per palette, add it here and the law in `tests/test_brand.py` will hold
#: the three lists (this one, `utils/hub.py` and `scripts/build_owner.py`) to it.
MARKS: tuple[str, ...] = ("logo.svg",)
FAVICONS: tuple[str, ...] = ("favicon.svg",)
CURSORS: tuple[str, ...] = ("cursor-default.svg", "cursor-pointer.svg")

#: Everything the site and the two static hosts ship out of `static/`.
ASSETS: tuple[str, ...] = MARKS + FAVICONS + CURSORS

#: Which mark a palette draws. A palette with no entry here gets the default,
#: which is the only safe fallback: a mark that does not exist is invisible.
MARK_FOR: dict[str, str] = {theme: "logo.svg" for theme in THEMES}
FAVICON_FOR: dict[str, str] = {theme: "favicon.svg" for theme in THEMES}

#: The identifiers that keep the codename this product grew up with. Not a
#: to-do list: each is a name that lives in a host's configuration, in a stored
#: row or in a browser's `localStorage`, where changing it costs a migration and
#: buys nothing a visitor can see.
INTERNAL_NAMES: tuple[str, ...] = (
    "MEOW_* environment variables",
    "the meow-bot and meow-owner Pages projects",
    "the meow_bank table",
    "the /meow and /meowsound commands",
    "the meow-theme localStorage key",
    "the window.MeowTheme global",
    "static/meow.css",
)


def mark_for(theme: str) -> str:
    """The logo a palette draws — the default mark when the palette is unknown."""
    return MARK_FOR.get(str(theme or ""), MARKS[0])


def favicon_for(theme: str) -> str:
    """The favicon a palette draws — the default when the palette is unknown."""
    return FAVICON_FOR.get(str(theme or ""), FAVICONS[0])


def wordmark() -> str:
    """The name as the header draws it: the word, and the dot the frame adds."""
    return NAME


def summary() -> dict:
    """Everything a brand sheet needs, from the one author.

    Returned as data rather than as markup so the page that renders it
    (`scripts/brand_sheet.py`) cannot disagree with the sheet about a value: the
    palette below is the module's, and the law asserts the stylesheet's matches.
    """
    return {
        "name": NAME,
        "tagline": TAGLINE,
        "initial": INITIAL,
        "accent": ACCENT_HEX,
        "accent_text": ACCENT_TEXT_HEX,
        "bg": BG_HEX,
        "palette": dict(PALETTE),
        "themes": tuple(THEMES),
        "marks": tuple(MARKS),
        "favicons": tuple(FAVICONS),
        "cursors": tuple(CURSORS),
        "retired": RETIRED_NAME,
    }
