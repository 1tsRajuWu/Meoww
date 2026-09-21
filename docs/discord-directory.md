# Listing the Bot in Discord's App Directory (Marketplace)

This guide provides the complete checklist, submission walkthrough, listing copy,
and policy guidelines for listing the Bot in Discord's **App Directory** (formerly
App Discovery / Marketplace), as well as notes on **Server Discovery**.

Everything below is verified against the current repository tree. Where a requirement
is Discord policy rather than a fact about this codebase, it is noted with the
current Discord Developer rules.

---

## 1. App Directory Eligibility Checklist

To be listed in Discord's App Directory, an application must satisfy technical,
security, and policy requirements:

| Discord Requirement | Status in this Repo | Implementation Details |
|---|---|---|
| **Privacy Policy URL** | ✅ Ready at `/privacy` | Served by `keep_alive.py` and `scripts/build_site.py` from `PRIVACY.md` |
| **Terms of Service URL** | ✅ Ready at `/terms` | Served from `TERMS.md` through the same renderer |
| **Global Slash Commands** | ✅ 92 distinct names | Declared in `utils/features.py` (`ROOT_COMMANDS`), synced via `bot.py` |
| **Command Descriptions** | ✅ Every command documented | Provided via `@app_commands.describe` and docstrings across `cogs/*.py` |
| **No Mandatory Privileged Intents** | ✅ Default off | Bot operates fully without privileged gateway intents (`bot.py`) |
| **No User Bots / Self-Bots** | ✅ Compliant | Built on discord.py as an official application |
| **Least Privilege Install Scope** | ✅ Ready | `MINIMAL_INVITE_PERMISSIONS` in `utils/discord_auth.py` |
| **AI Disclosure & Transparency** | ✅ Disclosed | Explicitly documented in `PRIVACY.md`, `TERMS.md`, and listing copy |
| **Verified Application** | ⚠️ Operator action | Requires bot in **75–100 servers** + Stripe ID verification |
| **Two-Factor Authentication (2FA)**| ⚠️ Operator action | Must be enabled on owner Discord account and team members |
| **Public Support Server** | ⚠️ Operator action | Permanent invite link configured in Developer Portal |
| **Media Carousel Assets** | ⚠️ Operator action | 1–5 screenshots (16:9 aspect ratio, 1280x720 minimum) |

---

## 2. Step-by-Step Portal Submission Walkthrough

All listing management takes place at the [Discord Developer Portal](https://discord.com/developers/applications):

### Step 1: Account & Bot Prerequisites
1. Ensure **Two-Factor Authentication (2FA)** is active on your Discord account (and all team members under Developer Teams).
2. Once the bot reaches the server threshold (75–100 guilds), complete **Application Verification** under **Settings → Verification** with government ID via Stripe Identity.

### Step 2: Configure General Information
1. Open your app and select **General Information**.
2. **App Icon**: Upload a clear 512x512 PNG/JPG icon.
3. **Privacy Policy URL**: Enter your live HTTPS endpoint (e.g. `https://your-domain.com/privacy`).
4. **Terms of Service URL**: Enter your live HTTPS endpoint (e.g. `https://your-domain.com/terms`).
5. **Tags**: Add relevant search tags (e.g. `moderation`, `economy`, `utility`, `community`, `ai`).

### Step 3: Configure Installation & Permissions
1. In the sidebar, select **Installation**.
2. Set **Install Link** to Discord Provided Link.
3. In **Default Install Settings**:
   - **Scopes**: Check `bot` and `applications.commands`.
   - **Permissions**: Use the minimal permission bits defined in `utils/discord_auth.py` (`MINIMAL_INVITE_PERMISSIONS`). **Do not select Administrator** for the directory listing, as blanket administrative permission requests are heavily scrutinized and frequently rejected. Owners who want full auto-management can still grant `INVITE_PERMISSIONS` separately via `/start`.

### Step 4: Configure App Directory Listing
1. In the sidebar, select **App Directory**.
2. Enter the prepared listing copy (see Section 3 below):
   - **Short Description**: 195 characters summarizing key features.
   - **Detailed Description**: Full markdown breakdown of features, commands, and disclosures.
   - **Primary Category**: Select *Community* or *Moderation*.
   - **Secondary Category**: Select *Utility*, *Economy*, or *Entertainment*.
   - **Support Server**: Paste a permanent, non-expiring invite link to your support server.
3. Upload **Media Carousel** screenshots showing commands and embeds in Discord.

### Step 5: Review & Publish
1. Review the automated pre-flight checklist in the portal.
2. Click **Enable Discovery** / **Publish to App Directory**.

---

## 3. Ready-to-Paste App Directory Listing Copy

This copy is directly aligned with `utils/features.py` and the legal documentation.
(Monitored by `tests/test_legal.py` to prevent drift between code and public text.)

### Short Description (195 characters)
```text
A community bot for moderation, tickets with transcripts, leveling, an economy with a shop and a live market, a starboard, a 365-day activity tracker, and a web dashboard with a message composer.
```
*(195 characters, fits within Discord's summary field limits.)*

### Detailed Description
```markdown
Meow is a moderation and community bot that stays out of your way. It brings 13 systems and 92 slash commands: automod and logging, tickets with transcripts and close-time ratings, leveling with automatic role rewards, a coin economy with a shop, loans and casino games, a live market, a starboard, social roleplay, fun commands and mini-games, a voice companion that sits with somebody alone in a voice channel, and a per-channel activity tracker keeping a 365-day window.

Everything is switched on and off per server from a web dashboard that signs in with Discord — staff see only the servers they actually administer, and nothing is ever published about a server's members: the public endpoints answer in counts, never in names.

### Key Systems & Commands
- **Moderation & Automod**: `/warn`, `/mute`, `/kick`, `/ban`, `/lockdown`, `/cases`, anti-invite and link filters.
- **Support Tickets**: Transcripts, ratings, and ticket claiming via buttons and modals.
- **Leveling & Activity**: XP rewards for text and voice, level-roles, and a 365-day per-channel tracker (`/activity me`, `/rank`).
- **Economy & Market**: `/balance`, `/daily`, `/pay`, shop, loans, and real-time public market quotes (`/market board`, `/market buy`, `/market chart`).
- **Dashboard & Composer**: Web-based module toggles and scheduled announcement composer (`/help` -> Open Dashboard).

### Transparency & AI Disclosure
Created and maintained with the help of AI coding assistants and autonomous AI agents. The runtime executes 100% deterministic Python code — no runtime LLM calls are made, and no user messages, prompts, or server metadata are sent to external AI providers or used for model training.

### Subscriptions & Future Monetization
Core features remain completely free for all communities. To offset infrastructure, database hosting, and operational scaling costs, optional paid subscriptions, premium perks, or enhanced quotas may be introduced in future updates. Any future paid offerings will be completely optional and clearly disclosed.

### Support & Community
Need assistance, found a bug, or have a suggestion? Join our support server or use `/bot bug` directly inside your server.
```

### Categories & Tags
- **Primary Category**: Community
- **Secondary Category**: Moderation (or Utility / Economy)
- **Tags**: `community`, `moderation`, `economy`, `utility`, `ai`

---

## 4. Discord AI Policy & Developer Policy Compliance

Discord maintains clear guidelines for applications involving Artificial Intelligence:

1. **Disclosure & Attribution**:
   Discord requires transparency when applications involve AI. While Meow does not execute an LLM at runtime, its source and ongoing operations were built and maintained with the help of AI coding assistants and autonomous AI agents. This is explicitly disclosed in the listing copy, `PRIVACY.md` (Section 7), and `TERMS.md` (Section 8).
2. **Data Privacy & Sub-Processors**:
   Discord policies strictly govern sending user messages or prompts to third-party AI APIs (such as OpenAI, Anthropic, or Google). Meow invokes **zero external AI model APIs at runtime**. No user prompts, channel text, or guild records are ever shared with AI providers or used for training (`docs/legal/sub-processors.md`).
3. **Safety & Content Standards**:
   Automated moderation filters and economy rules are designed deterministically to maintain community safety and prevent abuse.
4. **Least Privilege**:
   Permission requests follow minimal privilege principles via `MINIMAL_INVITE_PERMISSIONS` in `utils/discord_auth.py`, ensuring servers are not asked for unneeded administrative access.

---

## 5. Server Discovery (Support Server)

Discord Server Discovery is a separate feature for community servers (rather than apps):

| Discovery Gate | Requirement | Status |
|---|---|---|
| **Member Count** | **1,000+ members** | Milestone reached through community growth |
| **Server Age** | At least several weeks old | Standard requirement |
| **Content Setting** | Server is **not** age-restricted | Toggle in Server Settings → Overview |
| **Channel Activity** | Healthy retention across multiple channels | Checked automatically by Discord |
| **Discovery Setting** | Enable in Server Settings → Discovery | Activate once member threshold is met |

Listing the Bot in the App Directory first is the primary driver of organic traffic and user acquisition, which naturally paves the way for Server Discovery.
