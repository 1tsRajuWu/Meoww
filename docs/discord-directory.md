# Getting the Bot into Discord's App Directory (and the server into Discovery)

Two different listings, two different gates, and **neither is a code change**. The
Bot is technically ready for the first one; the second has a member count that no
amount of code can supply. This page is what to paste and what to fix, and — more
usefully — which requirements the code already satisfies and how each is held
there, so a future change cannot quietly un-satisfy one.

Everything below is verified against the current tree. Where a requirement is a
Discord policy rather than a fact about this repository, it is marked as such and
the date-free caveat is that Discord changes them; check the portal page you are
actually submitting on.

---

## 1. App Directory — what Discord requires, and where this repo stands

| Discord's requirement | This repository | Held by |
|---|---|---|
| A **Privacy Policy URL** | ✅ live at `/privacy` | `PRIVACY.md`, rendered by `keep_alive` + `scripts/build_site.py` |
| A **Terms of Service URL** | ✅ live at `/terms` | `TERMS.md`, same |
| **Global slash commands** with descriptions | ✅ 90 distinct names | `utils/features.py` (`ROOT_COMMANDS`) |
| A **description** for the app and each command | ✅ every command has one | `cogs/*.py` decorators |
| The app **must not** require privileged intents to work | ✅ intents are off unless asked for | `bot.py` |
| **Not** a "user bot" / self-bot | ✅ | — |
| Publicly installable with a sensible **permission scope** | ⚠️ see below | `utils/discord_auth.py` invite links |
| A **support server** is not required but is strongly recommended | ⚠️ your call | — |

### The two things to check before submitting

1. **The invite scope.** The listing is judged on what an ordinary member gets,
   and `Administrator` is the single most common reason a bot app is turned down.
   Two links exist in `utils/discord_auth.py` — `INVITE_PERMISSIONS` (one bit:
   Administrator, for owners who will grant it) and `MINIMAL_INVITE_PERMISSIONS`
   (the explicit list in `MINIMAL_PERMISSION_BITS`, for owners who will not).
   Submit the **minimal** one as the primary install and keep Administrator as the
   documented alternative. That is a choice already offered on `/start`; it only
   needs making on the portal form.
2. **`SUMMARY` and `DETAILED_DESCRIPTION` in the portal must match this Bot.** The
   copy below is derived from the feature registry, so if a feature is added,
   re-derive it rather than editing the portal by hand — `python -m pytest
   tests/test_legal.py` and `tests/test_features.py` fail loudly if the registry
   and the published counts drift apart, which is the same drift a stale listing
   would be.

### Listing copy, derived from the registry

> **Short description** (195 characters, so it fits the portal's short field)
>
> A community bot for moderation, tickets with transcripts, leveling, an economy
> with a shop and a live market, a starboard, a 365-day activity tracker, and a
> web dashboard with a message composer.

> **Detailed description**
>
> Meow is a moderation and community bot that stays out of your way. It brings
> 12 systems and 90 slash commands: automod and logging, tickets with transcripts and
> close-time ratings, leveling with automatic role rewards, a coin economy with a
> shop, loans and casino games, a live market, a starboard, social roleplay, fun
> commands and mini-games, and a per-channel activity tracker keeping a 365-day
> window.
>
> Everything is switched on and off per server from a web dashboard that signs in
> with Discord — staff see only the servers they actually administer, and nothing
> is ever published about a server's members: the public endpoints answer in
> counts, never in names.

> **Tags**: Community, Moderation, Fun, Utility, Economy

### What the portal needs that only you can supply

The listing is submitted at <https://discord.com/developers/applications> →
your app → **App Directory**. None of this is reachable from the repository:

- A **verified** application is required for the Directory. Verification needs the
  app to be in **100 or more servers** and to have been created at least a few
  weeks ago. Check the current floor in the portal — it has moved more than once.
- **Two-factor authentication enabled** on the owner's Discord account.
- The **privacy policy and terms URLs** pasted into the app's General Information
  page (the same two addresses the Bot prints from `/privacy` and `/terms`).

---

## 2. Server Discovery — for the support server, not the Bot

Discord's Server Discovery is a separate product with its own gates, and the one
that decides it is not a setting:

| Requirement | Status |
|---|---|
| **1,000+ members** | ✋ this is the gate, and it is a real one |
| Server is **not** age-restricted | set in Server Settings → Overview |
| Server has a **description** and **category tags** | set in the same place |
| Discovery **enabled** in Server Settings → Discovery | — |
| No **NSFW** or Discord-TOS-violating content | — |
| Activity in **multiple** channels, not one | — |

Until the member count is there, Discovery is simply closed — so the honest plan is
to list the Bot in the App Directory first (that is where a bot's traffic comes
from; a directory listing is *why* a server grows), and treat Discovery as a
later milestone rather than something to configure now and wait on.

---

## 3. What the code already refuses to do, that a listing reviewer checks

A submission is read by someone looking for the failure modes below. Each is
already guarded, and the guard is named so it can be re-run:

- **Naming a real single deployment's server in public copy** — `/health` and the
  hub answer in **counts, never names**, because a name is a reconnaissance hint
  (`utils/config_audit.py`, `tests/test_config_audit.py`).
- **Publishing a source path from the site** — `utils/site_guard.py` probes the
  live site for repository paths on a schedule and alerts, because every page
  looked correct while `/bot.py` was once served from the site's own address.
- **Anything to do with money** — no billing is wired; `plans.selling_enabled()` is
  the one switch and it is off (`tests/test_selling_surface.py`). A directory
  listing for a paid app carries requirements a free one does not.
- **Command permissions** — every command carries the audience declared in
  `utils/permissions.py`, stamped before publishing, so what Discord offers matches
  what the command will actually let a member do (`tests/test_permissions.py`).
- **Requesting a privileged intent that was not granted** — a login with an
  ungranted intent is the most common way a bot dies at boot, and this one refuses
  to ask unless the switch is set (`bot.py`).
