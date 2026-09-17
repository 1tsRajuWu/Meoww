# Answering an access or deletion request

Whoever uses the Bot can be shown what it holds about them and can ask for it to
be deleted. This is the procedure that does it store by store — written down
because two details make the obvious version wrong:

1. **The Bot restores a backup on boot.** Deleting rows on the host and then
   restarting can bring them straight back from the cloud snapshot. Do the
   deletion on the live database *and* let the new state become the current
   backup, or edit a downloaded copy and upload that.
2. **A snapshot that still holds the data outlives the deletion.** Up to 5
   hourly snapshots are kept, so delete the ones written before the request was
   answered, or the data is still readable by anyone with the bucket key.

## 1. Show a member what is held

For the member's own view, the Bot already answers most of it: `/rank`,
`/activity me`, `/balance`, `/inventory`, `/zoo`, `/market portfolio`,
`/warnings`. For a formal request, copy the live database off the host and query
it read-only:

```bash
sqlite3 bot.db ".mode insert" \
  "SELECT * FROM user_levels WHERE guild_id=:guild AND user_id=:member;"
```

The tables keyed to one member (`user_id`, unless noted): `activity_messages`,
`activity_voice`, `custom_quotes`, `economy`, `inventory`, `item_effects`,
`loans`, `market_holdings`, `market_trades`, `user_animals`, `user_levels`,
`warnings`, `tempvoice_channels` (`owner_id`), `giveaway_entries`,
`scheduled_messages` (`created_by`), `marriages` (`user1_id` / `user2_id`),
`mod_cases` (`user_id` and `moderator_id`), `ticket_feedback` (`author_id` and
`claimed_by`).

## 2. Delete one member's data

Run against the **live** database (or a copy you then upload), with a fresh
backup taken first:

```sql
-- per-guild, per-member stores
DELETE FROM activity_messages WHERE guild_id=:guild AND user_id=:member;
DELETE FROM activity_voice    WHERE guild_id=:guild AND user_id=:member;
DELETE FROM economy           WHERE guild_id=:guild AND user_id=:member;
DELETE FROM user_levels       WHERE guild_id=:guild AND user_id=:member;
DELETE FROM inventory         WHERE guild_id=:guild AND user_id=:member;
DELETE FROM item_effects      WHERE guild_id=:guild AND user_id=:member;
DELETE FROM user_animals      WHERE guild_id=:guild AND user_id=:member;
DELETE FROM market_holdings   WHERE guild_id=:guild AND user_id=:member;
DELETE FROM market_trades     WHERE guild_id=:guild AND user_id=:member;
DELETE FROM loans             WHERE guild_id=:guild AND user_id=:member;
DELETE FROM warnings          WHERE guild_id=:guild AND user_id=:member;
DELETE FROM custom_quotes     WHERE guild_id=:guild AND user_id=:member;
DELETE FROM marriages         WHERE guild_id=:guild AND (user1_id=:member OR user2_id=:member);
-- text the member wrote, and records that name them
DELETE FROM scheduled_messages WHERE guild_id=:guild AND created_by=:member;
DELETE FROM ticket_feedback    WHERE guild_id=:guild AND (author_id=:member OR claimed_by=:member);
DELETE FROM mod_cases          WHERE guild_id=:guild AND user_id=:member;
```

Decide `mod_cases` deliberately: a case is the server's moderation record, and
deleting it erases what a moderator did as much as what the member did. Keeping
the case while clearing the reason is a defensible middle ground.

**Then make it stick:** wait for the next upload (about 25 seconds) so the
trimmed database becomes the current snapshot, then delete every snapshot written
before it — in the Supabase dashboard, or with `utils/cloud_sync._storage_delete`.
A restart before that prunes nothing and can restore what you just deleted.

Rows that live in Discord rather than the database go with their messages: a
ticket transcript is a file in a channel, a starboard pin is a message, a bug
report is a message in a bug channel. Delete the message or the channel.

## 3. Delete a whole server's data

Everything keyed by `guild_id` (`activity_*`, `economy`, `user_levels`,
`warnings`, `mod_cases`, `tickets`, `scheduled_messages`, `starboard_posts`,
`marriages`, `user_animals`, `market_*`, `guild_settings`, `guild_modules`,
`level_roles`, `level_settings`, `shop`, `starboard_settings`, `tempvoice_*`,
`reaction_roles`, `giveaways`, `giveaway_entries`, `music_247`), then the
snapshot step above. Removing the Bot from the server stops new collection
immediately but does **not** delete what already exists — only this does.

## 4. Requests the member can answer themselves

| Want | Command |
|---|---|
| my warnings gone | `/clearwarnings` (staff) |
| my balance reset | `/eco-admin reset` (owner) |
| my level reset | `/resetlevels` (owner) |
| a quote I submitted | `/customquote delete` |
| my marriage | `/divorce` |
| my pets | `/sellanimals` |
| a queued or scheduled message | cancel it in the dashboard |

## 5. Retention, so a request can be answered honestly

| Store | Goes away on its own |
|---|---|
| Activity messages and voice rows | after 365 days (`cogs/tracker.py::RETENTION_DAYS`) |
| Dashboard message history — queued, sent, per-channel result | after 90 days (`utils/scheduled.py::HISTORY_DAYS`) |
| Cloud snapshots | oldest pruned beyond 5, hourly (`utils/cloud_sync.py`) |
| Everything else | kept until deleted, because that is the feature |

## 6. Nothing here is legal advice

This is what the software does, written down so a request can be answered with
evidence rather than a guess. What you owe the person asking depends on where you
and they are: a deletion request may carry obligations (and time limits) this
procedure does not describe.
