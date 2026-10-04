# Shrek Recall

Run start.bat on your laptop. It runs the bot as @shrekpobot using its own identity. Keep the laptop awake and connected. Close the terminal or press Ctrl+C to stop. A process lock prevents two instances from running at once. If the bot is running in the background, stop.bat stops only the project process recorded in bot.pid; after stopping, use start.bat normally.

## Add to another group

Add @shrekpobot as an administrator. It automatically starts archiving when it receives the next ordinary message or service update. No command, group ID, private channel, or activation step is required. The PRIVATE testing destination is excluded by default. New groups do not require changes to config.json. Basic groups and supergroups are supported, although live checks have been performed in a supergroup. Channels are not monitored.

Members should know messages are archived for 24 hours and can be restored after deletion. The bot restores messages from when it began receiving them; it cannot recover older content it never saved. Losing administrator status pauses checks; a cached role may take up to five minutes to expire.

## Detection and restoration

Incoming messages and edits are saved to archive.sqlite3. Photos and files are downloaded to media/ with two concurrent transfers, up to 100 MB per attachment by default. Media deleted before downloading finishes may be unavailable; the bot then attempts to reuse the saved Telegram media reference. Protected messages are skipped. Text, photos, documents, GIFs, stickers, voice and video messages preserve their original media attributes. Polls are recreated with their question, options and voting settings; votes start fresh and the original timer is removed. Quizzes whose correct answer is unavailable are restored as text. Contacts, locations and service messages are not fully reconstructed. A log entry records download or restoration failures.

Deletion events trigger verification. Batch reads also check the newest 200 saved messages about every two seconds and rotate through older messages in batches every five seconds. Older sweep time grows with the archive. All groups share a paced read queue and server FLOOD_WAIT responses are honored. Exact response timing is not guaranteed.

Two explicit empty reads are required before restoration. Permission, network and incomplete-response failures never count as deletion. Duplicate restoration is suppressed across polling and event paths. A short text is posted as a single reaction GIF with the saved text and sender name in its caption. For media, the reaction GIF comes first, followed by the media with its original caption. Empty media captions omit the name-and-text line on the GIF. Attribution refers to the original sender; Telegram does not identify who deleted a message. Sending still follows group rate limits, so simultaneous deletions can delay posts.

Archives persist across restarts and are reconciled again. Rows and associated cached files expire 24 hours after the original message date. Pending restoration failures remain available for retry. API sends do not have idempotency keys: a lost acknowledgement or crash between sending and saving progress can produce duplicates. Reconnect and permission problems are logged in mtproto.log.

## Configuration

Credentials are already configured locally. Keep config.json, shrek-bot.session and archive.sqlite3 private. Never share the session file: it grants bot access. First-time setup requires Python 3.10 or newer; start.bat creates a local .venv and installs the pinned Telethon dependency if needed.

- retention_hours: 24 by default.
- recent_check_seconds: 2 by default.
- max_media_mb: 100 by default.
- excluded_group_ids: group IDs that must not be monitored.
- gif_path: cringe-shrek.gif by default.

After changing settings, restart the bot. No hosted service is required: your laptop is the host. It stops being online when the process stops, the laptop sleeps, or internet access is lost.

The older forwarding implementation is preserved as bot_forwarding_backup.py. Its PRIVATE forwarding destination is no longer used. Its old messages.sqlite3 archive is preserved separately; the new version starts a fresh archive and does not silently reinterpret that database.

The reaction GIF is cached as a Telegram document after its first send; subsequent posts reuse it. Changing the local GIF invalidates the cache on restart. The final cached-GIF diagnostic restored a saved fixture in 1.029 seconds without duplicate posts; this is an observation, not a timing guarantee.


