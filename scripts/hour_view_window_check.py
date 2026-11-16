"""Should the hour view's volatility window be shorter than an hour, and variable? (2026-11-15, user: "should we make the hour view more like half an hour, or fifteen; it should be variable")

One question, same samples (CLAUDE.md rule 4): the hour view is curverisk.long_chance, the chance a side pays from the Kalshi perp's distance to its window-open reference in units of the perp's own movement. Its movement is the root-mean-square of the last HOUR of one-minute moves. This reads the same chance with the movement taken over 15, 30 and 60 minutes, and over a window that varies with the time left (4 x the time left, between 15 and 60 minutes: a short market looks at a short past), and scores each against what the market did: Brier score and log loss, and the chance that sat in the hold zone (0.30 to 0.60, around the 0.4 bar) against how often the side paid there. Samples: every 15-minute crypto market of the last 24 hours at 600, 450, 300 and 180 seconds left (YES chance; the result from the last taped quote: 0.95 and over paid, 0.05 and under did not; no clean close = skipped). Not split by curve shape. A check of calibration, not a rule: the window is only changed after a stated rule is scored forward.

Run on the host: python scripts/hour_view_window_check.py (HOURS=24 to change the look back).
"""
import bisect
import datetime
import math
import os
import time

from kalshi_moonshot import config, curverisk, db
from kalshi_moonshot.watcher import perp_index

HOURS = float(os.environ.get("HOURS", "24"))
LEFTS = (600.0, 450.0, 300.0, 180.0)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
curverisk.MIN_MOVES = 12  # the live rule wants 30 one-minute moves; a 15-minute window has 15, so this check asks for 12 for every window alike
s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()
variants = {"15 min": lambda left: 0.25, "30 min": lambda left: 0.5, "60 min": lambda left: 1.0, "variable: 4 x time left, 15 to 60 min": lambda left: min(max(4.0 * left / 3600.0, 0.25), 1.0)}
samples = {k: [] for k in variants}
used = skipped = 0
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    g = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, now - HOURS * 3600 - 4000)).fetchall()
    ts, vs = [float(x[0]) for x in g], [float(x[1]) for x in g]
    closes = sorted({int((now - HOURS * 3600) // 900 * 900 + 900 * k) for k in range(int(HOURS * 4) + 1) if (now - HOURS * 3600) // 900 * 900 + 900 * k < now - 900})
    for close in closes:
        opened = close - 900
        i = bisect.bisect_left(ts, opened)
        if i >= len(ts) or ts[i] - opened > 60.0:
            skipped += 1
            continue
        ref = vs[i]
        tk = tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? AND ts BETWEEN ? AND ? LIMIT 1", (ser + "-%", close - 120, close)).fetchone()
        if not tk:
            skipped += 1
            continue
        q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (tk[0],)).fetchone()
        paid = 1 if float(q[0]) >= 0.95 else 0 if float(q[1]) <= 0.05 else None
        if paid is None:
            skipped += 1
            continue
        used += 1
        for left in LEFTS:
            t = close - left
            a = bisect.bisect_left(ts, t - 3700)
            b = bisect.bisect_right(ts, t)
            for name, fn in variants.items():
                p = curverisk.long_chance(ts[a:b], vs[a:b], t, ref, "yes", left, hours=fn(left))
                if p is not None:
                    samples[name].append((min(max(p, 1e-4), 1 - 1e-4), paid))
print(f"markets used {used}, skipped {skipped}; samples per variant:", {k: len(v) for k, v in samples.items()})
print("\nvariant: n | Brier | log loss | hold zone 0.30 to 0.60: n, mean chance, share that paid")
for name, x in samples.items():
    if not x:
        print(f"  {name}: no readings (too few one-minute moves in the window)")
        continue
    n = len(x)
    brier = sum((p - y) ** 2 for p, y in x) / n
    ll = -sum(math.log(p if y else 1 - p) for p, y in x) / n
    z = [(p, y) for p, y in x if 0.30 <= p < 0.60]
    zt = f"{len(z):4d}, {sum(p for p, _ in z) / len(z):.3f}, {sum(y for _, y in z) / len(z):.3f}" if z else "none"
    print(f"  {name:40s}: {n:4d} | {brier:.4f} | {ll:.4f} | {zt}")
print("DONE")
