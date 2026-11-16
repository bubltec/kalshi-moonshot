"""The hour view with the market's own life so far as its first segment (2026-11-15 night, user: "start with the view from now - market start, 15 after that, 60, 180")

One question, same samples as scripts/multiscale_hour_check.py (every 15-minute window of the last DAYS days for 11 coins, read at 600, 450, 300 and 180 seconds left, so 5, 7.5, 10 and 12 minutes into the market; the YES chance from the perp's distance to its window-open reference over rate x sqrt(time left); the outcome the perp's direction over the window; Brier, log loss and the paired log-loss change against the live 60-minute block, over all samples and over the last third in time). The movement rate is the log-mean (geometric mean) of the rates of these segments:
  nested: the market so far (from its open to now, e minutes), the last 15, the last 60 and the last 180 minutes, every one ending now;
  adjacent: the market so far [open, now], then the 15 minutes before the open, the 60 before that, the 180 before that (separate stretches, going back in time, none overlapping);
  cumulative: windows that start at the open and reach back by 15, 60 and 180 minutes past it (e, e+15, e+60, e+180, every one ending now), and the chained lengths e, e+15, e+75, e+255;
  and, for reference, the nested set without the market-so-far segment (15, 60, 180), the market-so-far alone, and the market-so-far with 60 and 180.
Each segment rate is the root-mean-square one-minute move in it over sqrt(60). Not split by curve shape. A check of the chance's quality, not a rule.

Run on the host: python scripts/market_start_segments_check.py (DAYS=3 to change the look back).
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
samples = []  # (close, left, cushion, paid, e, m2)
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, now - DAYS * 86400 - 3 * 3600 - 3600)).fetchall()
    ts = [float(r[0]) for r in rows]
    lv = [math.log(float(r[1])) for r in rows]

    def level_at(t):
        i = bisect.bisect_right(ts, t) - 1
        return lv[i] if i >= 0 and t - ts[i] <= 120.0 else None
    first = int((now - DAYS * 86400) // 900 * 900 + 900)
    for close in range(first, int(now - 60) // 900 * 900 + 1, 900):
        ref, end = level_at(close - 900), level_at(close)
        if ref is None or end is None or ref == end:
            continue
        paid = 1 if end > ref else 0
        for left in LEFTS:
            t = close - left
            cur = level_at(t)
            marks = [level_at(t - 60.0 * k) for k in range(0, 262)]
            if cur is None or any(m is None for m in marks):
                continue
            m2 = [(marks[k] - marks[k + 1]) ** 2 for k in range(261)]
            samples.append((close, left, cur - ref, paid, max(2, int(round((900.0 - left) / 60.0))), m2))
samples.sort(key=lambda r: r[0])


def rate(m2, a, b):
    """The rate over the one-minute moves a (newest, 0) to b (older) minutes back, per sqrt-second: root-mean-square over sqrt(60)."""
    x = m2[a:b]
    if len(x) < max(2, min(b - a, 30)):
        return None
    v = sum(x) / len(x) / 60.0
    return math.sqrt(v) if v > 0 else None


def geo(rs):
    rs = [r for r in rs if r]
    return math.exp(sum(math.log(r) for r in rs) / len(rs)) if rs else None


variants = {
    "block 60 (live)": lambda m2, e: rate(m2, 0, 60),
    "market so far alone": lambda m2, e: rate(m2, 0, e),
    "nested: so far, 15, 60, 180": lambda m2, e: geo([rate(m2, 0, e), rate(m2, 0, 15), rate(m2, 0, 60), rate(m2, 0, 180)]),
    "nested: 15, 60, 180 (no market-so-far)": lambda m2, e: geo([rate(m2, 0, 15), rate(m2, 0, 60), rate(m2, 0, 180)]),
    "nested: so far, 60, 180": lambda m2, e: geo([rate(m2, 0, e), rate(m2, 0, 60), rate(m2, 0, 180)]),
    "adjacent: so far, 15 before, 60 before, 180 before": lambda m2, e: geo([rate(m2, 0, e), rate(m2, e, e + 15), rate(m2, e + 15, e + 75), rate(m2, e + 75, e + 255)]),
    "adjacent: so far, 15 before, 60 before": lambda m2, e: geo([rate(m2, 0, e), rate(m2, e, e + 15), rate(m2, e + 15, e + 75)]),
    "cumulative: so far, +15, +60, +180 past the open (e, e+15, e+60, e+180)": lambda m2, e: geo([rate(m2, 0, e), rate(m2, 0, e + 15), rate(m2, 0, e + 60), rate(m2, 0, e + 180)]),
    "cumulative chain: e, e+15, e+75, e+255": lambda m2, e: geo([rate(m2, 0, e), rate(m2, 0, e + 15), rate(m2, 0, e + 75), rate(m2, 0, e + 255)]),
    "cumulative: so far, +15, +60 (e, e+15, e+60)": lambda m2, e: geo([rate(m2, 0, e), rate(m2, 0, e + 15), rate(m2, 0, e + 60)]),
}


def chance(cush, left, r):
    return min(max(N.cdf(cush / (r * math.sqrt(left))), 1e-4), 1 - 1e-4)


def losses(rows, f):
    out = []
    for close, left, cush, paid, e, m2 in rows:
        r = f(m2, e)
        out.append(None if not r else -math.log(chance(cush, left, r) if paid else 1 - chance(cush, left, r)))
    return out


base = losses(samples, variants["block 60 (live)"])
cut = int(len(samples) * 2 / 3)
print(f"samples {len(samples)}; the last third starts at sample {cut}")
print("\nvariant | log loss | change vs live (mean +- se) on all samples | on the last third")
for name, f in variants.items():
    l = losses(samples, f)
    ok = [i for i in range(len(samples)) if l[i] is not None and base[i] is not None]
    d = [l[i] - base[i] for i in ok]
    md = sum(d) / len(d); se = math.sqrt(sum((a - md) ** 2 for a in d) / (len(d) - 1) / len(d))
    dl = [l[i] - base[i] for i in ok if i >= cut]
    ml = sum(dl) / len(dl); sl = math.sqrt(sum((a - ml) ** 2 for a in dl) / (len(dl) - 1) / len(dl))
    print(f"  {name:52s}| {sum(l[i] for i in ok) / len(ok):.4f} | {md:+.4f} +- {se:.4f} (n {len(ok)}) | {ml:+.4f} +- {sl:.4f}")
print("DONE")
