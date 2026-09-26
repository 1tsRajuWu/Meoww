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
  preferred the *running application's* name from Discord and fell back to a
  literal in the middle of a 10,000-line file. That fallback is the brand, and it
  was spelled out where nobody would look for it — and picking the live name is
  the half that had to go, because the application this product runs as is
  verified and Discord will not rename it from the API, so the page read a name
  nobody could change. This module is the one author now.

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
#: What the product is called. `keep_alive._bot_display_name()` returns this and
#: nothing else, so the name a page draws and the name a card is signed with
#: cannot come from two places. It used to defer to the live Discord
#: application's name, which reads well until the application cannot be renamed:
#: a verified application refuses the API rename, so the field went stale and
#: every backend-rendered page kept announcing the name this product retired.
NAME = "DaCat"

#: What it is, in one line — the sentence a `og:description` or a footer wants.
TAGLINE = "One bot that runs the server."

#: Who holds the copyright, and the year. Here rather than typed into a footer
#: because the footer is rendered by five builds (the site, the four Pages
#: projects, the hub) and the licence at the root of this tree names the same
#: holder: one fact, one author, so a year that moves moves everywhere at once.
COPYRIGHT_HOLDER = "Himanshu Raj"
COPYRIGHT_YEAR = 2026

#: The letter the header's tile draws before the wordmark. Derived from the name
#: by the law at the bottom of `tests/test_brand.py`, so an initial that no longer
#: matches is a failure rather than a detail.
INITIAL = "D"

#: The name this product used before the rebrand. Nothing a person reads may
#: still say it; the law asserts that over the templates, and the two places that
#: are allowed to mention it are this line and the CHANGELOG's own history.
RETIRED_NAME = "Keystone"

#: What the machine calls itself when something asks. The Space's ``/health`` is
#: served by `app.py` as a literal rather than rendered from a template, so the
#: name is *derived* here and the release check compares the served value with
#: it: a Space still announcing the retired name is a host a whole rebrand behind,
#: and that is a fact worth failing a release on (see `scripts/ship.py`).
GATEWAY_SERVICE = f"{NAME.lower()}-gateway"

# --------------------------------------------------------------------------- #
# The accent
# --------------------------------------------------------------------------- #
#: The value every card the bot sends carries, as Discord wants it: one integer,
#: `0xRRGGBB`. Read by `utils/style.ACCENT` rather than typed again there.
ACCENT = 0xFF5D3A

#: The same colour as the sheets state it. `ACCENT` and this *are* one fact, and
#: the law asserts the sheet still spells it this way.
ACCENT_HEX = "#ff5d3a"

#: `--accent-rgb`, for the glows and rings that need the channels separately.
ACCENT_RGB = "255 93 58"

#: The **on-state** green, and it is not the accent. A toggle grid is the product
#: this site is built around, and a switch has two readings that must never be
#: mistakable for one another: *do this* (a coral fill, one per screen, the thing
#: Von Restorff's law spends) and *this is on* (a green glow, on every control
#: that is on, which is why it cannot be the accent — a screen of coral switches
#: would make the primary action indistinguishable from the state of a row).
#: It is the same value the semantic `good` token carries, because "on" and
#: "working" are one reading in this product.
ON_HEX = "#34d399"
ON_RGB = "52 211 153"

#: The shade anything *written* in the accent uses. A fill and a line of text are
#: held to different contrast floors (3:1 against 4.5:1), so one value cannot
#: pass both — which is why the sheet has carried two names since long before this
#: rebrand, and why a rebrand has to move both.
ACCENT_TEXT_HEX = "#ff8a70"
ACCENT_TEXT_RGB = "255 138 112"

#: The ink that goes on an accent **fill**. A coral fill is a light colour, so
#: the label on the primary button is the page's own black — white on coral
#: measures 3.0:1 and would fail the floor the button's label is held to,
#: which is the same two-floors problem `--accent-text` exists for, one layer
#: further in. It is :data:`BG_HEX` because that is what the dark palette
#: already paints and the mark is drawn to sit on exactly this pair.
ACCENT_INK_HEX = "#0a0a0d"

#: The page ground. One literal, here, because a page's `<meta name="theme-color">`
#: is served as markup rather than resolved from the sheet — the script is
#: deliberately forbidden from rewriting it (see `tests/test_theme.py`), so the
#: value needs an author that is not "whatever somebody typed into the head".
BG_HEX = "#0a0a0d"
BG_DEEP_HEX = "#050507"

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
    "surface-card": "#131318",
    "surface-sunken": "#0b0b0f",
    "surface-hover": "#1a1a21",
    "text-primary": "#f2f3f5",
    "text-secondary": "#b9bcc4",
    "text-muted": "#93969e",
    "text-invert": BG_HEX,
    "accent": ACCENT_HEX,
    "accent-text": ACCENT_TEXT_HEX,
    "accent-rgb": ACCENT_RGB,
    "accent-text-rgb": ACCENT_TEXT_RGB,
    "accent-ink": ACCENT_INK_HEX,
    "accent-hover": "#f04a26",
    "on": ON_HEX,
    "on-rgb": ON_RGB,
    "good": ON_HEX,
    "warn": "#fbbf24",
    "danger": "#f87171",
    "line": "#24242c",
    "line-strong": "#34343e",
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

#: Self-hosted typefaces. CSP is `font-src 'self'`, so these files travel with
#: every static host rather than being fetched from a CDN — and they are named
#: here so `hub.SHARED_ASSETS` and the owner build cannot forget them.
FONTS: tuple[str, ...] = (
    # The two faces the design system was rebuilt on: Archivo driven through its
    # condensed width axis for display copy and figures, Inter for everything a
    # person reads. Both are OFL, both are variable, and both were vendored from
    # Google's own distribution rather than a re-hosted copy — with the licence
    # text travelling beside them (see `fonts/OFL.txt`), which is what OFL asks
    # for and what a `font-src 'self'` policy needs.
    "fonts/archivo-var-latin.woff2",
    "fonts/archivo-var-latin-ext.woff2",
    "fonts/inter-var-latin.woff2",
    "fonts/inter-var-latin-ext.woff2",
    "fonts/OFL-archivo.txt",
    "fonts/OFL-inter.txt",
    # The faces the old system shipped, kept because `/market`, the sign-in
    # doors and the per-server dashboard still read them: a sheet that names a
    # font this build stopped copying is a page that renders in the fallback with
    # nothing in any log to say why.
    "fonts/poppins-400.woff2",
    "fonts/poppins-500.woff2",
    "fonts/poppins-600.woff2",
    "fonts/poppins-700.woff2",
    "fonts/jetbrains-mono-500.woff2",
)

#: Everything the site and the two static hosts ship out of `static/`.
ASSETS: tuple[str, ...] = MARKS + FAVICONS + FONTS

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
    "the meow-* Pages projects, retired with their names (the four that replaced "
    "them are dacat, dacatowner, dacatdashboard and dacatmarket)",
    "the meow_bank table",
    "the /meow and /meowsound commands",
    "the meow-theme localStorage key",
    "the window.MeowTheme global and the window.Meow wake hook",
    "static/meow.css",
    "the X-Meow-Client-IP and X-Meow-Proxy headers the Pages workers set, which "
    "two hosts have to agree about",
    "the Meow* HTTP User-Agents the bot sends when it fetches something",
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
        "on": ON_HEX,
        "bg": BG_HEX,
        "palette": dict(PALETTE),
        "themes": tuple(THEMES),
        "marks": tuple(MARKS),
        "favicons": tuple(FAVICONS),
        "retired": RETIRED_NAME,
    }
