# Shrek Recall is online

Project: `C:\Users\dasry\Desktop\Game Projects\TelegramBot`

## Normal use

1. Add [@shrekpobot](https://t.me/shrekpobot) to the real group and make it an administrator.
2. Keep the laptop awake and connected to the internet.
3. After a reboot or stopping the bot, double-click start.bat in the project folder.

No activation command, group-ID configuration, or forwarding channel is required. The bot automatically registers an administrator group when its next message or service update arrives. Only new messages received by the bot are archived.

The bot is already running in the background. Opening start.bat now will report that it is already running. To switch to a visible terminal, first double-click stop.bat, then start.bat. Stop only one instance and keep that terminal open.

## Defaults

- Retention: 24 hours after each original message timestamp.
- Method: MTProto authenticated as the existing bot, deletion events plus batch reconciliation.
- Newest 200 saved IDs per group: checks approximately every two seconds.
- Older IDs: batches rotate every five seconds; full-sweep time depends on archive size and group count.
- GIF: cringe-shrek.gif, cached on Telegram for reuse.
- PRIVATE: excluded from monitoring; no longer needed for forwards.
- Media: downloads up to 100 MB per attachment, with two concurrent transfers.

Short text is restored in the GIF caption. Longer text and ordinary media are sent separately from the GIF. Download failures, protected content, structured message types, network outages and Telegram flood waits limit restoration. A two-second response is not guaranteed; sending multiple restorations is paced to respect group limits.

## Validation

Five meaningful local tests passed: message IDs remain separate across groups, edits preserve archive status, only explicit empty responses count as candidates, two confirmations suppress duplicate restoration, errors never cause reposting, and a new administrator group is discovered without configuring its ID. Some tests cover more than one assertion.

Live human-message tests confirmed capture, edit capture, deletion verification and GIF restoration. The final cached-GIF smoke test restored its archived diagnostic fixture in 1.029 seconds, produced exactly one matching restoration and reused the cached Telegram document. The diagnostic fixture and its restored GIF were cleaned up.

The previous implementation and configuration are preserved locally as backups. The bot uses a new archive.sqlite3; content already expired from the old archive cannot be recovered. Credentials and the bot session remain in the project folder and should not be shared.
