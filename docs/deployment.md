# Deployment Setup Guide

## Current Deployment Configuration

### 1. GitHub
- Repository: discord-bot
- Branch: main
- Status: Active and synced
- Deployment: Manual deployment to Render

### 2. Render Configuration
- Service: Web service (meow-discord-bot)
- Runtime: Python 3.11
- Plan: Free tier
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

### 3. Supabase Storage (Database Backup)
- Purpose: Persistent database storage across deployments
- Configuration:
  - SUPABASE_URL: Supabase project URL
  - SUPABASE_SERVICE_KEY: Service role key for storage access
  - Bucket: bot-backups
  - Backup files:
    - bot.db (current state)
    - bot-db-YYYYMMDD-HHMMSS.db (snapshots)
  - Retention: 5 snapshots, rotated hourly
  - Sync interval: Every 25 seconds

### 4. Cloudflare Pages (the public site)
- Project: `meow-bot` → https://meow-bot.pages.dev
- Source: this GitHub repository, connected through Pages' own Git integration,
  deploying on every push to `main`. `Site` in GitHub Actions builds the same
  output but **cannot publish** — its token has `Pages Read` and not
  `Pages Write` — so the Git integration is what actually ships the site, and a
  failing `Site` run is not a failing deploy.
- **Build command:** `pip install -r requirements.txt && python scripts/build_site.py --out _site --proxy`
- **Build output directory:** `_site`

Both of those settings are load-bearing, and the output directory is the one to
watch. Cloudflare Pages is handed a directory and told *everything in it is
public*. Leave the build command and output directory empty and Pages publishes
the **repository root** instead — which is a public copy of `bot.py`, `utils/`,
`cogs/`, `render.yaml` and `.env.example` at the site's own address. That is not
a theory: it is what this project was doing, and the Pages build log said so in
plain words (`No build command specified. Skipping build step.`, then
`Uploading... (185/185)` — the repository has exactly 185 tracked files). No
secret value leaked, because every secret lives in the host's environment rather
than in the tree, but the source was readable by anyone until it was caught.

`scripts/build_site.py` now **refuses** to build into the repository root, so
the other half of that mistake cannot be made quietly.

The output directory must contain `_worker.js` and `_routes.json` (the builder
writes both). They are what reverse-proxies `/login`, `/dashboard`, `/servers`,
`/api`, `/market` and `/health` through to Render, so the dashboard and the
sign-in page live on the Pages domain rather than 404ing there. A deployment
missing them answers **404** for every one of those paths — which is how this
was diagnosed.

#### Checking it from a terminal

```bash
# Every one of these must be 404: the source is not a web page.
for p in /bot.py /utils/cloud_sync.py /cogs/economy.py /render.yaml /.env.example; do
  printf '%-24s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "https://meow-bot.pages.dev$p"
done

# These must NOT be 404: the proxy is running.
for p in /login /health; do
  printf '%-24s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "https://meow-bot.pages.dev$p"
done
# /dashboard and /servers answer 302 -> /login when signed out, which is correct.
```

## Environment Variables Required

### Discord Bot
- `DISCORD_TOKEN`: Bot token from Discord Developer Portal

### Supabase Storage
- `SUPABASE_URL`: Your Supabase project URL
- `SUPABASE_SERVICE_KEY`: Service role key (not anon key)

### Optional
- `ENABLE_MESSAGE_CONTENT_INTENT=1`: Enable message content intent
- `ENABLE_MEMBERS_INTENT=1`: Enable members intent
- `ERROR_WEBHOOK_URL`: Error reporting webhook
- `DASHBOARD_HTTPS=1`: force the dashboard's session cookie to `Secure`. Render
  and Railway are detected automatically; set this when you terminate TLS
  yourself (the server binds `0.0.0.0`, so without it a plaintext request to the
  dashboard port would carry the session cookie)

## Deployment Process

### Initial Setup
1. Create Supabase project
2. Create storage bucket named "bot-backups"
3. Get SUPABASE_URL and SUPABASE_SERVICE_KEY
4. Add environment variables to Render
5. Deploy from GitHub

### Automatic Backup System
The bot includes automatic cloud sync:
- **Restore**: On startup, restores latest backup from Supabase
- **Backup**: Every 25 seconds, uploads database changes
- **Snapshots**: Hourly timestamped snapshots
- **Retention**: Keeps 5 most recent snapshots
- **Protected Tables**: economy, user_levels, mod_cases, warnings, activity_messages, activity_voice

### Deployment Safety Features
- Single-writer lease prevents conflicts
- Database shrinkage detection
- Automatic retry on network failures
- SIGTERM handler for clean shutdown
- WAL mode for concurrent access

## Recent Changes (Activity Tracking Fixes)

### Bug Fixes Applied
1. **Admin-only permissions**: Activity commands now require Administrator permission
2. **New channel tracking**: Added listener for on_guild_channel_create to track new channels
3. **Data persistence**: Added activity_messages and activity_voice to COUNTED_TABLES
4. **Error handling**: Added permission error handlers for better user feedback

### Files Modified
- `cogs/tracker.py`: Added admin checks, new channel listener, error handlers
- `utils/permissions.py`: Changed activity commands to ADMIN level
- `utils/cloud_sync.py`: Added activity tables to backup protection

## Verification Steps

### Pre-deployment
1. Test activity tracking locally
2. Verify admin permissions work
3. Test new channel creation tracking
4. Check database backup system

### Post-deployment
1. Verify bot connects to Discord
2. Test activity commands with admin account
3. Check Supabase bucket for backups
4. Monitor logs for backup sync status

## Troubleshooting

### Backup Issues
- Check SUPABASE_URL and SUPABASE_SERVICE_KEY are set
- Verify bucket "bot-backups" exists in Supabase
- Check logs for "[CLOUD SYNC]" messages

### Permission Issues
- Verify user has Administrator permission
- Check Discord Developer Portal for bot permissions
- Review utils/permissions.py for command access levels

### Tracking Issues
- Verify tracker module is enabled in guild settings
- Check logs for "[TRACKER]" messages
- Ensure bot has necessary intents

## Monitoring

### Key Log Messages
- `[TRACKER]`: Activity tracking events
- `[CLOUD SYNC]`: Backup/restore operations
- `[PERMISSIONS]`: Command permission changes
- `[SYNC]`: Command tree synchronization

### Health Checks
- Bot online status
- Command permissions
- Backup sync frequency
- Database integrity