"""Isolated MTProto diagnostic. Never reads/modifies the live archive DB.

Uses the configured bot identity in the configured testing supergroup.
Only its own clearly labelled diagnostic messages are deleted automatically.
No credentials or user message contents are written into the report.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import time

from telethon import TelegramClient, events, functions, types, utils, errors

def classify_messages(requested_ids, messages):
    """Missing IDs are inconclusive; only explicit MessageEmpty is a candidate."""
    requested = set(requested_ids)
    existing = {m.id for m in messages if isinstance(m, (types.Message, types.MessageService)) and m.id in requested}
    empty = {m.id for m in messages if isinstance(m, types.MessageEmpty) and m.id in requested}
    return {'existing_ids': sorted(existing), 'empty_ids': sorted(empty),
            'unreturned_ids': sorted(requested - existing - empty)}

async def run(args):
    path = Path(args.config).resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    api_id = os.environ.get('TELEGRAM_API_ID') or config.get('api_id')
    api_hash = os.environ.get('TELEGRAM_API_HASH') or config.get('api_hash')
    token = os.environ.get('TELEGRAM_BOT_TOKEN') or config.get('bot_token')
    if not api_id or not api_hash or not token:
        print('Missing api_id/api_hash. Save them in config.json before the live tests.')
        return 2
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    session = output.parent / 'mtproto-diagnostic'
    report = {'mode': 'bot MTProto', 'group_id': config['group_id'],
              'reads': [], 'delete_events': [], 'new_messages': [], 'tests': {},
              'limits_note': 'Short conservative test; cannot establish a guaranteed rate limit.'}
    def save():
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    client = TelegramClient(str(session), int(api_id), api_hash,
                            flood_sleep_threshold=0, request_retries=0,
                            timeout=15, connection_retries=2)
    own_ids = []
    deletion_started = {}
    @client.on(events.MessageDeleted(chats=config['group_id']))
    async def deleted(event):
        now = time.monotonic()
        report['delete_events'].append({'chat_id': event.chat_id, 'ids': event.deleted_ids,
             'own_test_ids': [mid for mid in event.deleted_ids if mid in own_ids],
             'latency_since_own_delete_request_seconds': {str(mid):round(now-deletion_started[mid], 3)
                       for mid in event.deleted_ids if mid in deletion_started}})
        save()
        print('DELETION EVENT', event.deleted_ids, flush=True)
    @client.on(events.NewMessage(chats=config['group_id']))
    async def received(event):
        report['new_messages'].append({'id': event.id, 'from_self': event.out,
                                     'sender_is_bot': bool(getattr(event.sender, 'bot', False))})
        save()
        print('NEW MESSAGE', event.id, flush=True)

    async def batch(peer, ids, label):
        start = time.monotonic()
        row = {'label': label, 'requested_ids': list(ids), 'count': len(ids)}
        try:
            result = await client(functions.channels.GetMessagesRequest(
                    channel=utils.get_input_channel(peer),
                    id=[types.InputMessageID(mid) for mid in ids]))
            row.update(classify_messages(ids, result.messages))
            row['ok'] = True
        except errors.FloodWaitError as error:
            row.update(ok=False, error='FloodWaitError', wait_seconds=error.seconds)
            report['reads'].append(row)
            save()
            print('FLOOD WAIT: stopping test and respecting server backoff', flush=True)
            raise
        except errors.RPCError as error:
            row.update(ok=False, error=type(error).__name__)
        row['elapsed_seconds'] = round(time.monotonic()-start, 3)
        report['reads'].append(row)
        save()
        print('READ', label, 'count', len(ids), 'ok', row['ok'], flush=True)
        return row

    try:
        await client.start(bot_token=token)
        identity = await client.get_me()
        if not identity.bot:
            raise RuntimeError('Diagnostic session is not a bot; refusing to proceed')
        report['bot_username'] = identity.username
        peer = await client.get_input_entity(config['group_id'])
        report['tests']['entity_resolution'] = True
        save()
        print('Connected as bot @' + identity.username, flush=True)
        for index in range(5):
            message = await client.send_message(peer, f'[MTProto diagnostic {index+1}/5] Temporary test message; automatic cleanup follows.')
            own_ids.append(message.id)
            await asyncio.sleep(3.2)
        before = await batch(peer, own_ids, 'five-known-live-messages')
        report['tests']['reads_own_live_messages'] = before.get('existing_ids') == sorted(own_ids)
        await asyncio.sleep(3)
        # Include a range with real gaps/older IDs; report shape without logging contents.
        for requested_size in (20, 100, 200):
            ids = list(range(max(1, own_ids[-1]-requested_size+1), own_ids[-1]+1))
            await batch(peer, ids, f'range-up-to-{requested_size}-ids')
            await asyncio.sleep(3)
        targets = own_ids[1:3]
        for mid in targets:
            deletion_started[mid] = time.monotonic()
        await client.delete_messages(peer, targets, revoke=True)
        await asyncio.sleep(3)
        after = await batch(peer, own_ids, 'mixed-live-and-deleted')
        report['tests']['deleted_ids_return_empty'] = set(targets).issubset(after.get('empty_ids', []))
        report['tests']['surviving_controls_present'] = set(own_ids)-set(targets) == set(after.get('existing_ids', []))
        report['tests']['own_delete_event_received'] = any(set(targets).intersection(e['ids']) for e in report['delete_events'])
        save()
        print('Ready for human deletion test: send and delete a fresh text in the testing group.', flush=True)
        # Modest stable rate, not a stress test. Human messages are probed individually
        # alongside known diagnostic controls; no copied content or reposts.
        deadline = time.monotonic() + args.listen_seconds
        while time.monotonic() < deadline:
            candidates = [m['id'] for m in report['new_messages'] if not m['from_self'] and not m['sender_is_bot']]
            ids = list(dict.fromkeys(own_ids + candidates))[-100:]
            await batch(peer, ids, 'steady-read-and-human-message-check')
            await asyncio.sleep(args.poll_seconds)
        report['tests']['human_messages_seen'] = sorted(set(candidates)) if candidates else []
        report['tests']['own_delete_event_received'] = any(set(targets).intersection(e['ids']) for e in report['delete_events'])
        report['tests']['non_self_deletion_event_ids'] = sorted({mid for e in report['delete_events'] for mid in e['ids'] if mid not in own_ids})
        successful = [r['elapsed_seconds'] for r in report['reads'] if r['ok']]
        report['median_read_seconds'] = round(statistics.median(successful), 3) if successful else None
        report['completed'] = True
        save()
        return 0
    except Exception as error:
        # Avoid exception text that might include credentials or serialized requests.
        report['failure_type'] = type(error).__name__
        safe_description = str(error)
        for secret in (token, api_hash):
            safe_description = safe_description.replace(str(secret), '[redacted]')
        report['failure_description'] = safe_description
        save()
        print('Diagnostic stopped:', type(error).__name__, flush=True)
        return 1
    finally:
        if client.is_connected():
            if own_ids and report.get('failure_type') != 'FloodWaitError':
                try:
                    await client.delete_messages(config['group_id'], own_ids, revoke=True)
                except Exception:
                    report['cleanup_failed'] = True
                    save()
            await client.disconnect()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', default='mtproto-test-results.json')
    parser.add_argument('--listen-seconds', type=int, default=60)
    parser.add_argument('--poll-seconds', type=float, default=5)
    args = parser.parse_args()
    if args.poll_seconds < 2 or args.listen_seconds > 120:
        parser.error('Use at least 2-second intervals and at most 120-second listening tests')
    raise SystemExit(asyncio.run(run(args)))
