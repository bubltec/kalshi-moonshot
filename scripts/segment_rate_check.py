"""The hour view from the four 15-minute segments of the last hour (2026-11-15, user: "No just use every 15 mins in the hour" after "each segment should be a factor of the next ... a differential or logarithmic")

One question, same samples as scripts/decay_window_check.py (every 15-minute window of the last DAYS days for 11 coins, read at 600, 450, 300 and 180 seconds left; the YES chance from the perp's distance to its window-open reference over rate x sqrt(time left); the outcome the perp's direction over the window; scored by Brier and log loss against the live reading, with the paired difference and its standard error). The movement rate is built from the last hour in four 15-minute segments: the mean square one-minute move of each segment s0 (newest) to s3. Variants: the four equal (that is the live 60-minute block); each segment a factor f of the next older one (f 0.5, 0.7, 0.85 give the recent segments more weight, 1.2 and 1.5 are the reverse as controls); the newest segment alone; the newest segment stepped on by the log trend between it and the one before (exp(log r0 + beta x (log r0 - log r1)), beta 0.5 and 1, the step clipped to a factor of two); and a line through the four log-rates read one segment ahead. A block of 120 minutes is kept as the reference that beat the live reading. Not split by curve shape. A check of the chance's quality, not a rule.

Run on the host: python scripts/segment_rate_check.py (DAYS=3 to change the look back).
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


def seg(m2):
    """Mean square one-minute move in each 15-minute segment of the last hour, most recent first."""
    return [sum(m2[15 * k:15 * k + 15]) / 15.0 for k in range(4)]


def weighted(m2, f):
    s2 = seg(m2)
    w = [f ** k for k in range(4)]
    return math.sqrt(sum(a * b for a, b in zip(w, s2)) / sum(w) / 60.0)


def log_extrapolate(m2, beta):
    """The newest segment's rate stepped on by the log-trend of the segment rates: exp(log r0 + beta x (log r0 - log r1)), the step clipped to a factor of 2 either way."""
    s = [math.sqrt(a / 60.0) for a in seg(m2)]
    if min(s) <= 0:
        return None
    step = max(-math.log(2.0), min(math.log(2.0), math.log(s[0]) - math.log(s[1])))
    return s[0] * math.exp(beta * step)


def log_trend(m2):
    """A least-squares line through the four segment log-rates (oldest to newest), read one segment ahead of the newest."""
    s = [math.log(math.sqrt(a / 60.0)) for a in reversed(seg(m2))] if min(seg(m2)) > 0 else None
    if s is None:
        return None
    x = [0, 1, 2, 3]
    mx, my = 1.5, sum(s) / 4
    slope = sum((a - mx) * (b - my) for a, b in zip(x, s)) / sum((a - mx) ** 2 for a in x)
    step = max(-math.log(2.0), min(math.log(2.0), slope))
    return math.exp(s[3] + step)


def variants(m2, left):
    v = {"block 60 (live): four segments, equal": rate_block(m2, 60), "block 120": rate_block(m2, 120)}
    for f in (0.5, 0.7, 0.85, 1.2, 1.5):
        v[f"segments, each x{f} of the next older"] = weighted(m2, f)
    v["newest segment alone (15)"] = rate_block(m2, 15)
    for b in (0.5, 1.0):
        v[f"newest segment stepped by log trend x{b}"] = log_extrapolate(m2, b)
    v["log-linear trend of the four segments"] = log_trend(m2)
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
base = samples["block 60 (live): four segments, equal"]
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
