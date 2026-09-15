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