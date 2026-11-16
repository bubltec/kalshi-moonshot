"""Do the crypto indexes move in waves (direction shifting inside minutes) or in trends? (2026-11-15, user: "crypto moves in waves and directions shift in minutes not hours; it's more complex than a fade, it's a differential or logarithmic")

The hour view and the hourly width factor treat the last hour as one block. This measures the time scale of the direction itself, from the Kalshi perp reference's own tape (one reading a minute: the first tick of each minute, NOT the average of the minute, which by itself puts a lag-1 autocorrelation of 0.25 into a random walk; the last three days, each of the 11 coins):
  variance ratio VR(q) = variance of the q-minute log return / (q x the variance of the one-minute return). 1 = a random walk; under 1 = the moves give back (waves); over 1 = they carry on (trends). Read at q of 2, 3, 5, 8, 15, 30 and 60 minutes.
  the autocorrelation of one-minute returns at lags 1 to 10 minutes, and of non-overlapping 5-minute returns at lags 1 to 4 (5 to 20 minutes): negative = the next stretch turns back against this one.
Also the memory of VOLATILITY: the autocorrelation of the size of one-minute moves at lags 1 to 60 minutes: how long a quiet or wild spell lasts, which is what a window for the market's width has to match. The coins are pooled (mean of each statistic, and how many of the 11 are under 1). A reading of the market's own behavior, not a rule; the coin names are never used, only the pooled curve.

Run on the host: python scripts/wave_variance_ratio.py (DAYS=3 to change the look back).
"""
import math
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.watcher import perp_index

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
DAYS = float(os.environ.get("DAYS", "3"))
s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()


def var(x):
    m = sum(x) / len(x)
    return sum((a - m) ** 2 for a in x) / (len(x) - 1)


def acf(x, lag):
    m = sum(x) / len(x)
    d = sum((a - m) ** 2 for a in x)
    return sum((x[i] - m) * (x[i + lag] - m) for i in range(len(x) - lag)) / d if d > 0 else 0.0


vr = {q: [] for q in (2, 3, 5, 8, 15, 30, 60)}
ac1 = {k: [] for k in range(1, 11)}
ac5 = {k: [] for k in range(1, 5)}
acabs = {k: [] for k in (1, 2, 3, 5, 10, 15, 20, 30, 60)}  # volatility memory: autocorrelation of the size of one-minute moves
used = 0
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts AS m, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, now - DAYS * 86400)).fetchall()
    px = [float(r[1]) for r in rows]
    if len(px) < 600:
        continue
    used += 1
    lp = [math.log(p) for p in px]
    r1 = [lp[i + 1] - lp[i] for i in range(len(lp) - 1)]
    v1 = var(r1)
    for q in vr:
        rq = [lp[i + q] - lp[i] for i in range(0, len(lp) - q)]
        vr[q].append(var(rq) / (q * v1))
    for k in ac1:
        ac1[k].append(acf(r1, k))
    ra = [abs(x) for x in r1]
    for k in acabs:
        acabs[k].append(acf(ra, k))
    r5 = [lp[i + 5] - lp[i] for i in range(0, len(lp) - 5, 5)]
    for k in ac5:
        ac5[k].append(acf(r5, k))
print(f"coins used {used}, about {len(px)} one-minute readings each over {DAYS:g} days (gaps in the tape count as one longer step)")
print("variance ratio VR(q): mean over coins | coins under 1")
for q, v in vr.items():
    print(f"  q = {q:2d} min: {sum(v) / len(v):.3f} | {sum(1 for a in v if a < 1)} of {len(v)}")
print("autocorrelation of one-minute returns at lag 1..10 minutes (mean over coins):", " ".join(f"{k}:{sum(v) / len(v):+.3f}" for k, v in ac1.items()))
print("autocorrelation of non-overlapping 5-minute returns at lag 1..4 (5 to 20 minutes):", " ".join(f"{k * 5}m:{sum(v) / len(v):+.3f}" for k, v in ac5.items()))
print("volatility memory: autocorrelation of |one-minute return| at lag (minutes):", " ".join(f"{k}:{sum(v) / len(v):+.3f}" for k, v in acabs.items()))
print("DONE")
