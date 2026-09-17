# Voice Reward Debugging Guide

## Common Issues and Solutions

### 1. Voice Rewards Not Working

**Symptoms**: Users in voice channels are not receiving XP or coins.

**Possible Causes**:

#### A. Leveling Module Disabled
- Check if the leveling module is enabled for your guild
- Run `/help` and tap **⚡ Open Dashboard** to reach the web admin panel
- Verify "Leveling & XP" is enabled (green card)

#### B. Voice Reward Settings Set to 0
- Check voice reward rates using `/levelsettings`
- Default: 2 coins/min, 3 XP/min
- If set to 0, no rewards will be given

#### C. Anti-AFK Checks Too Strict
The voice reward system has several anti-AFK gates:

1. **Company Gate**: Need at least 1 human in channel (including yourself)
   - Solo users earn at 35% rate (reduced but not zero)
   - Multiple users earn higher rates

2. **Presence Gate**: Users must be able to participate
   - Deafened users earn 0
   - Server-muted users earn 0  
   - Self-muted users earn 50%
   - Self-video/streaming users earn 20% bonus

3. **Freshness Gate**: Channel must show recent activity
   - Fresh channel (recent activity): 100% rate
   - Stale channel (20+ minutes no activity): Reduced rate
   - Dead channel (6+ hours no activity): 0% rate

### 2. Debug Logging

The bot now includes detailed logging for voice rewards. Look for these log messages:

```
[LEVELS] Leveling cog loaded, starting VC reward loop...
[LEVELS] VC reward loop started successfully
[LEVELS] VC reward loop running for {guild_name} - vc_coins: {rate}, vc_xp: {rate}
[LEVELS] VC #{channel_name} - {count} human(s) present
[LEVELS] VC #{channel_name} - Company factor: {factor}
[LEVELS] VC #{channel_name} - Channel age: {age}s, Freshness: {factor}
[LEVELS] Member {username} - Presence factor: {factor}
[LEVELS] Member {username} - Combined factor: {factor}
[LEVELS] Member {username} - Base payout: {coins} coins, {xp} XP
[LEVELS] Member {username} - Scaled payout: {coins} coins, {xp} XP
[LEVELS] Member {username} - Paid: +{coins} coins, +{xp} XP
```

### 3. Testing Voice Rewards

#### Manual Test:
1. Join a voice channel with at least one other person
2. Ensure you are not deafened or server-muted
3. Send a message in a text channel (this refreshes the channel)
4. Wait 60 seconds for the reward loop to run
5. Check logs for payment messages
6. Use `/rank` to see if voice time increased

#### Diagnostic Commands:
- `/rank` - Check your voice time and XP
- `/levelsettings` - Check voice reward rates
- `/leaderboard voice` - See who has the most voice time

### 4. Configuration Check

Run these commands to verify configuration:

```
/levelsettings
```

Check the output shows:
- System Status: 🟢 Enabled
- Voice (VC) Rewards: up to `X` 🪙 + `Y` XP / min

If rates are 0, set them higher:
```
/levelsettings vc_coins_per_min:2
```

### 5. Common Fixes

#### Issue: "No voice time tracking"
**Solution**: 
- Ensure leveling module is enabled
- Check voice reward rates are not 0
- Verify users are not deafened/server-muted

#### Issue: "Solo users earn nothing"
**Solution**:
- This is by design - solo users earn at 35% rate
- Encourage group voice chat for full rewards
- This prevents AFK farming

#### Issue: "Channel marked as dead"
**Solution**:
- Send a message in text channel while in VC
- Toggle mute/unmute to refresh channel
- Wait for next reward cycle (60s)

### 6. Log Analysis

If voice rewards still don't work, check the logs:

1. Look for `[LEVELS]` messages
2. Verify VC reward loop is running
3. Check if your guild is being processed
4. See if you're being evaluated for rewards
5. Check if you're being skipped and why

Example of working logs:
```
[LEVELS] VC reward loop running for My Server - vc_coins: 2, vc_xp: 3
[LEVELS] VC #General - 2 human(s) present
[LEVELS] VC #General - Company factor: 0.78
[LEVELS] VC #General - Channel age: 5s, Freshness: 1.00
[LEVELS] Member JohnDoe - Presence factor: 1.00
[LEVELS] Member JohnDoe - Combined factor: 0.78
[LEVELS] Member JohnDoe - Base payout: 2 coins, 3 XP
[LEVELS] Member JohnDoe - Scaled payout: 2 coins, 3 XP
[LEVELS] Member JohnDoe - Paid: +2 coins, +3 XP
```

### 7. Support

If issues persist after checking all above:
1. Check the bot has proper permissions in voice channels
2. Verify the bot is not rate-limited
3. Ensure the database is writable
4. Check for any error messages in logs
5. Review the deployment status on Render