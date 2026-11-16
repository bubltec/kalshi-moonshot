"""Does a log-decay window read the hour view better than a block hour? (2026-11-15, user: yes, run it; after "crypto moves in waves ... directions shift in minutes ... it's a differential or logarithmic")

One question, same samples (CLAUDE.md rule 4). The hour view is the chance a side pays from the perp's distance to its window-open reference in units of the perp's own movement over the time left (curverisk.long_chance). Its movement is the root-mean-square of the last hour's one-minute moves, every minute weighted alike. This reads the same chance with the movement taken as
  block 60 min (the live reading), block 15 min, block 5 min;
  the variable block (4 x the time left, between 15 and 60 minutes);
  log-decay weights 1 / (1 + age / tau) over the last 180 minutes, tau = 5, 15 and 30 minutes (recent minutes count most, old ones fade slowly, not at an edge);
  the geometric blend of the 5-minute and the 60-minute rate, sqrt(r5 x r60): the short rate over the long one used as a differential factor on the long rate.
Samples: every 15-minute window of the last DAYS days for each of 11 coins, read at 600, 450, 300 and 180 seconds left; the YES chance against whether the perp finished the window above where it opened (the direction the market settles on, to within the index's difference from the perp: far more windows than the taped last quote gives). Scored by Brier and log loss, each against the live reading (60-minute block) with the paired difference and its standard error; and by how the chance sat in the hold zone (0.30 to 0.60) against how often the side paid. Not split by curve shape. A check of the chance's quality, not a rule.

Run on the host: python scripts/decay_window_check.py (DAYS=3 to change the look back).
"""
import bisect
import math
import os
import time
from statistics import NormalDist

from kalshi_moonshot import config, db
from kalshi_moonshot.watcher import perp_index

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
DAYS = float(os.environ.get("DAYS", "3"))
LEFTS = (600.0, 450.0, 300.0, 180.0)
N = NormalDist()
s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()


def rate_block(m2, minutes):
    x = m2[:minutes]
    return math.sqrt(sum(x) / len(x) / 60.0) if len(x) >= min(minutes, 10) and x else None


def rate_decay(m2, tau, span=180):
    x = m2[:span]
    if len(x) < 30:
        return None
    w = [1.0 / (1.0 + (i + 0.5) / tau) for i in range(len(x))]
    return math.sqrt(sum(a * b for a, b in zip(w, x)) / sum(w) / 60.0)


def variants(m2, left):
    r60, r15, r5 = rate_block(m2, 60), rate_block(m2, 15), rate_block(m2, 5)
    v = {"block 60 (live)": r60, "block 15": r15, "block 5": r5, "block 120": rate_block(m2, 120), "block 180": rate_block(m2, 180),
         "variable block (4 x left, 15-60)": rate_block(m2, int(min(max(4.0 * left / 60.0, 15.0), 60.0))),
         "decay tau 5": rate_decay(m2, 5.0), "decay tau 15": rate_decay(m2, 15.0), "decay tau 30": rate_decay(m2, 30.0),
         "blend sqrt(r5 x r60)": math.sqrt(r5 * r60) if r5 and r60 else None}
    return v


samples = {}
windows = 0
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, now - DAYS * 86400 - 14400)).fetchall()
    ts = [float(r[0]) for r in rows]
    lv = [math.log(float(r[1])) for r in rows]
    full = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, now - 2 * 3600)).fetchall() if False else None

    def level_at(t):
        i = bisect.bisect_right(ts, t) - 1
        return lv[i] if i >= 0 and t - ts[i] <= 120.0 else None
    first = int((now - DAYS * 86400) // 900 * 900 + 900)
    for close in range(first, int(now - 60) // 900 * 900 + 1, 900):
        o, c = level_at(close - 900 + 30.0), level_at(close - 30.0)
        # the reference is the perp at the open, the outcome its value at the close
        ref, end = level_at(close - 900), level_at(close)
        if ref is None or end is None or ref == end:
            continue
        windows += 1
        paid = 1 if end > ref else 0
        for left in LEFTS:
            t = close - left
            now_l = level_at(t)
            if now_l is None:
                continue
            marks = [level_at(t - 60.0 * k) for k in range(0, 182)]
            if any(m is None for m in marks):
                continue
            m2 = [(marks[k] - marks[k + 1]) ** 2 for k in range(181)]
            cush = now_l - ref
            for name, r in variants(m2, left).items():
                if r is None or r <= 0:
                    continue
                p = N.cdf(cush / (r * math.sqrt(left)))
                samples.setdefault(name, {})[(ser, close, left)] = (min(max(p, 1e-4), 1 - 1e-4), paid)
base = samples["block 60 (live)"]
keys = [k for k in base if all(k in samples[n] for n in samples)]
print(f"windows {windows}, samples scored on the same keys for every variant: {len(keys)}")
print("\nvariant | Brier | log loss | log-loss change vs live (mean +- se; negative = better) | hold zone 0.30-0.60: n, mean chance, share that paid")
for name, d in samples.items():
    x = [d[k] for k in keys]
    n = len(x)
    brier = sum((p - y) ** 2 for p, y in x) / n
    ll = [-math.log(p if y else 1 - p) for p, y in x]
    lb = [-math.log(base[k][0] if base[k][1] else 1 - base[k][0]) for k in keys]
    diff = [a - b for a, b in zip(ll, lb)]
    md = sum(diff) / n
    se = math.sqrt(sum((a - md) ** 2 for a in diff) / (n - 1) / n)
    z = [(p, y) for p, y in x if 0.30 <= p < 0.60]
    zt = f"{len(z):5d}, {sum(p for p, _ in z) / len(z):.3f}, {sum(y for _, y in z) / len(z):.3f}" if z else "none"
    print(f"  {name:34s}| {brier:.4f} | {sum(ll) / n:.4f} | {md:+.4f} +- {se:.4f} | {zt}")
print("DONE")
