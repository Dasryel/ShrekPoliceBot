"""Offline steady-cohort simulation of bot.py's due-time queue. No network."""
import argparse
import heapq
import json
import math
import random
from pathlib import Path

def trial(count, spacing=3.1, latency=0.3, seed=0):
    rng = random.Random(seed)
    # Fixed cohort models the steady active count: arrival rate * retention.
    # Assumes messages remain eligible throughout each measured deletion.
    heap = [(0.0, i, 0) for i in range(count)]
    heapq.heapify(heap)
    slot = spacing + 2 * latency + 0.1
    deletion = max(60, count * slot * 3) + rng.uniform(0, max(30, count * slot))
    target = rng.randrange(count)
    clock = 0.0
    checks = 0
    while heap:
        due, mid, misses = heapq.heappop(heap)
        start = max(clock, due)
        if mid == target and start >= deletion:
            if misses == 2:
                # Saved text send, then GIF send, sharing the send limiter.
                return start + latency + spacing + latency - deletion
            duration = latency
            next_due = start + duration + 3
            misses += 1
        else:
            duration = 2 * latency  # Forward plus delete the test copy.
            next_due = start + 30
        heapq.heappush(heap, (next_due, mid, misses))
        clock = start + duration + spacing + 0.1
        checks += 1
        if checks > count * 20 + 100:
            raise RuntimeError('Simulation did not converge')

def scenarios():
    rows = []
    for rate in (0.5, 1, 5, 10):
        for window in (20, 60, 180, 1440):
            count = max(1, math.ceil(rate * window))
            outcomes = [retention_trial(rate, window, seed=seed) for seed in range(30)]
            samples = sorted(x for x in outcomes if x is not None)
            rows.append(dict(messages_per_minute=rate, tracking_minutes=window,
                             active_messages=count,
                             estimated_round_seconds=max(30, count * 3.8),
                             detected_trials=len(samples), total_trials=30,
                             simulated_mean_response_seconds=round(sum(samples)/len(samples), 1) if samples else None,
                             simulated_p95_response_seconds=round(samples[math.ceil(.95*len(samples))-1], 1) if samples else None))
    return dict(assumptions=dict(destination='supergroup', send_spacing_seconds=3.1,
                latency_per_request_seconds=0.3, scheduler_overhead_seconds=0.1,
                check_interval_seconds=30, confirmation_delay_seconds=3,
                trials_per_scenario=30, cohort='regular arrivals, original expiry enforced; delete one random eligible message after 3 tracking windows of warmup',
                exclusions='No 429s, outages, bursts, unsupported messages, or concurrent deletions. This models queue capacity, not actual Telegram throttling. Response statistics include detected trials only.'), scenarios=rows)

def retention_trial(rate, minutes, seed=0):
    rng = random.Random(seed)
    window = minutes * 60
    arrivals = 60 / rate
    deletion = window * 3
    heap = []
    clock = 0.0
    next_arrival = 0.0
    serial = 0
    target = None
    target_expiry = None
    confirmed = False
    while True:
        # Receive messages even while the checker is occupied.
        horizon = clock
        if not heap:
            horizon = max(clock, next_arrival)
        elif heap[0][0] > clock:
            horizon = min(heap[0][0], max(clock, next_arrival))
        clock = horizon
        while next_arrival <= clock:
            heapq.heappush(heap, (next_arrival + 30, serial, 0, next_arrival + window))
            next_arrival += arrivals
            serial += 1
        if clock >= deletion and target is None:
            eligible = [item for item in heap if item[3] > clock]
            if not eligible:
                return None
            chosen = rng.choice(eligible)
            target, target_expiry = chosen[1], chosen[3]
            deletion = clock
        if target is not None and clock > target_expiry and not confirmed:
            return None
        if not heap or heap[0][0] > clock:
            continue
        due, mid, misses, expiry = heapq.heappop(heap)
        if clock > expiry and misses < 2:
            continue
        start = clock
        if mid == target:
            if misses == 2:
                return start + 3.7 - deletion
            duration = 0.3
            next_due = start + duration + 3
            misses += 1
            if misses == 2:
                confirmed = True
        else:
            duration = 0.6
            next_due = start + 30
        heapq.heappush(heap, (next_due, mid, misses, expiry))
        clock = start + duration + 3.1 + 0.1

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='simulation-results.json')
    args = parser.parse_args()
    data = scenarios()
    Path(args.output).write_text(json.dumps(data, indent=2), encoding='utf-8')
    for row in data['scenarios']:
        if row['messages_per_minute'] == 1:
            print(row)
