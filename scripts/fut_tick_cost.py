"""What the Hyperliquid tick feed costs the hot path (2026-11-08, user: "ensure it doesn't impact our current buying and selling speed").

Measures on this machine, no network and no database:
 1. microseconds per Tape.add_fut call (the futflow socket thread's work per trade: a memory note in ticks.history, the per-second fold, the millisecond queue);
 2. the read the exits and entries make on the same store (variants.unit_index over 15 minutes of an index, ticks.history.window) while a thread feeds 5,000 perp trades a second into it, against the same reads with nothing feeding;
 3. the cost of Tape.add (a quote, as the Kalshi socket thread does) with and without that feed running.
Reported as p50 / p99 / max in microseconds.

    .venv/bin/python scripts/fut_tick_cost.py
"""

import tempfile
import threading
import time
from pathlib import Path

from kalshi_moonshot import tape as tape_mod
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot import variants

tmp = Path(tempfile.mkdtemp())
tape = tape_mod.Tape(tmp / "t.db")
now = time.time()
for k in range(1800):  # half an hour of a 1 Hz settlement index
    ticks_mod.history.note("BRTI", now - 1800 + k, 80000.0 + (k % 7) * 3.0)
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def show(label, xs):
    print(f"   {label:58s} p50 {pct(xs, 0.5) * 1e6:8.1f} us  p99 {pct(xs, 0.99) * 1e6:8.1f} us  max {max(xs) * 1e6:9.1f} us  (n={len(xs)})")


def timed(fn, n):
    out = []
    for _ in range(n):
        a = time.perf_counter()
        fn()
        out.append(time.perf_counter() - a)
    return out


stop = threading.Event()
sent = [0]


RATE = [0]


def feeder():
    rate = RATE[0]
    k = 0
    t = time.time()
    while not stop.is_set():
        c = COINS[k % 5]
        tape.add_fut(c, t + k / rate, 80000.0 + (k % 97), 0.1, k % 2 == 0)
        k += 1
        sent[0] = k
        if k % 500 == 0:
            time.sleep(500 / rate)


print("1. one trade on the socket thread:")
k = [0]


def one():
    k[0] += 1
    tape.add_fut(COINS[k[0] % 5], now + k[0] * 0.0002, 80000.0 + (k[0] % 97), 0.1, k[0] % 2 == 0)


show("Tape.add_fut", timed(one, 100000))
tape.flush(now=now + 1e6)

print("2. the exits' reads of the shared store:")
show("variants.unit_index (15 minutes), nothing feeding", timed(lambda: variants.unit_index("BRTI", now), 300))
show("ticks.history.window (55 minutes), nothing feeding", timed(lambda: ticks_mod.history.window("BRTI", now - 3300, now), 300))
for rate, label in ((300, "300 trades/s (about the busiest second of a day: BTC 258)"), (5000, "5,000 trades/s (twenty times that)")):
    stop.clear()
    RATE[0], sent[0] = rate, 0
    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    time.sleep(1.0)
    show(f"variants.unit_index, {label}", timed(lambda: variants.unit_index("BRTI", now), 300))
    print(f"3. a Kalshi quote on its socket thread, feed at {rate}/s:")
    q = [0]

    def quote():
        q[0] += 1
        tape.add({"kind": "quote", "ticker": "KXBTC15M-X", "ts": now + q[0] * 0.01, "bid": 0.40 + (q[0] % 5) * 0.01, "ask": 0.45, "last": 0.42, "sts": 1})

    show("Tape.add (quote), feed running", timed(quote, 5000))
    stop.set()
    th.join()
    print("   trades fed while measuring:", sent[0])
print("3b. a Kalshi quote on its socket thread, nothing feeding:")
q = [0]


def quote_idle():
    q[0] += 1
    tape.add({"kind": "quote", "ticker": "KXBTC15M-X", "ts": now + q[0] * 0.01, "bid": 0.40 + (q[0] % 5) * 0.01, "ask": 0.45, "last": 0.42, "sts": 1})


show("Tape.add (quote), feed stopped", timed(quote_idle, 5000))
print("DONE")
