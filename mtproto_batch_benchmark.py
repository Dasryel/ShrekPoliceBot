"""Bounded read-only benchmark. No sends, deletions, or history enumeration."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from telethon import TelegramClient, functions, types, utils, errors
from mtproto_probe import classify_messages

async def main(args):
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    output = Path(args.output)
    report = {'reads': [], 'no_send_or_delete_operations': True,
              'note': 'Requests include unavailable and future IDs; this tests request size, not 200 live messages. Not a guaranteed quota test.'}
    client = TelegramClient(str(output.parent / 'mtproto-diagnostic'), config['api_id'], config['api_hash'],
                            receive_updates=False, flood_sleep_threshold=0, request_retries=0)
    try:
        await client.start(bot_token=config['bot_token'])
        me = await client.get_me()
        if not me.bot:
            raise RuntimeError('Expected a bot session')
        peer = await client.get_input_entity(config['group_id'])
        for count in [1,20,100,200] + [200]*10:
            ids = list(range(1, count+1))
            start = time.monotonic()
            result = await client(functions.channels.GetMessagesRequest(utils.get_input_channel(peer), [types.InputMessageID(i) for i in ids]))
            shape = classify_messages(ids, result.messages)
            row = {'requested_count': count, 'elapsed_seconds': round(time.monotonic()-start,3),
                   'existing_count': len(shape['existing_ids']), 'empty_count':len(shape['empty_ids']),
                   'unreturned_count': len(shape['unreturned_ids'])}
            ages = [(datetime.now(timezone.utc) - m.date).total_seconds()/60 for m in result.messages
                     if isinstance(m, types.Message) and getattr(m, 'date', None)]
            row['oldest_readable_normal_message_age_minutes'] = round(max(ages),1) if ages else None
            report['reads'].append(row)
            output.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print('Read', count, 'IDs in', row['elapsed_seconds'], 'seconds', flush=True)
            await asyncio.sleep(2)
        report['completed'] = True
        report['median_read_seconds'] = statistics.median(r['elapsed_seconds'] for r in report['reads'])
    except errors.FloodWaitError as error:
        report.update(stopped='FloodWaitError', wait_seconds=error.seconds)
        print('Flood wait received; stopping benchmark.', flush=True)
    except Exception as error:
        report['stopped'] = type(error).__name__
        print('Benchmark stopped:', type(error).__name__, flush=True)
    finally:
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        await client.disconnect()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    asyncio.run(main(parser.parse_args()))
