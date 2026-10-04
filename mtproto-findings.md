# Shrek Recall: live test findings

Tested on 4 October 2026 using the existing @shrekpobot identity in the Shrekpobot testing supergroup. The forwarding implementation was left running unchanged.

## What passed

- Bot authentication through MTProto; no personal-account session was used.
- Five known messages read together using channels.getMessages.
- Deleted diagnostic IDs returned explicit MessageEmpty placeholders; surviving controls remained readable.
- A new message from another member (ID 43) was received and its deletion produced a MessageDeleted event. Batch reads also returned an empty placeholder for that ID.
- Four initial read sizes and ten repeated 200-ID reads completed, with two-second pauses between reads and no observed flood waits. Median read duration in the larger-batch test was 0.0945 seconds. The eleven 200-ID requests took 0.085–0.119 seconds.
- Readable ordinary messages older than 63 minutes were returned. The existing 20-minute archive expiry is an application setting, not a Telegram 30-minute read limit.
- Temporary diagnostic messages were cleaned up.

## Limits of the evidence

The 200-ID requests contained only about 30 readable messages, with deleted, unavailable, and future IDs making up the rest. This establishes that a 200-ID request works in this group, not performance for 200 large messages or media payloads. No media was downloaded.

This was a short bounded test, not a measurement of a guaranteed rate ceiling. Telegram may impose dynamic flood waits with sustained traffic. Every flood wait must be honored.

The bot's own deletion event arrived 38.47 seconds after its deletion request. The other member's deletion event was observed, but the exact time they clicked Delete was not instrumented, so a two-second end-to-end response was not measured. Event delivery is a useful fast path, with batch reads needed for reconciliation.

Empty placeholders also represent otherwise unavailable messages. Only IDs actually received and saved by this bot should be candidates for restoration; access failures, request errors, and unreturned IDs must remain inconclusive. Check a candidate again before restoring it, and suppress duplicates across the event and polling paths.

## Recommended design

1. Run one Telethon session authenticated as the bot and save incoming messages and edits in SQLite immediately.
2. Subscribe to supergroup MessageDeleted events. For saved IDs, verify deletion with a direct read and queue a single restoration.
3. As a fallback, check up to 200 of the newest saved IDs every two seconds initially. This cadence passed the short test but should remain adaptive.
4. Check older saved IDs in rotating batches, targeting 30–60 seconds while capacity allows. Increase the sweep interval as the archive grows rather than accumulating a permanent backlog.
5. Start with 24-hour retention, configurable. Retention and check cadence are independent; receiving an event can identify an older saved message without sweeping the whole archive.
6. Honor FLOOD_WAIT globally for reads. Separately pace posts to the source group; message sending still has its own limits.
7. Use the GIF with a saved-text caption for short messages if desired, reducing two outgoing messages to one. Longer text and media may need separate posts.
8. Reconcile after reconnect, preserve the session and archive, retry transient errors, and deduplicate all restorations.

No PRIVATE forwarding group is necessary under this design. Around two-second responses are plausible in a quiet group, but a hard guarantee is not supported by these tests. Multiple reposts, flood waits, reconnects, and network delays can increase response time.

## Reference

- [MTProto bot authentication](https://core.telegram.org/api/bots)
- [channels.getMessages](https://core.telegram.org/method/channels.getMessages)
- [Retrieval guidance and empty placeholders](https://core.telegram.org/api/updates#recovering-gaps)
- [Telethon deletion events](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.messagedeleted.MessageDeleted)

The older forwarding simulation applies to the current implementation only. It does not describe MTProto batch-read capacity.
