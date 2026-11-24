"""Is 'a typical minute' (the one-second move scaled by sqrt(60)) a fair size for what the settlement index does in 10 seconds, on the 15-minute markets? (2026-11-24; user, on the DOGE 08:15 bet: "a typical minute is over 60s which isn't accurate in these 15m markets. things can change in 10s.")
QUESTION (one): for each coin's settlement index over the last 3 days of tape, how big are the moves over 1, 10, 30, 60, 180 and 300 seconds against what the one-second move scaled by the square root of time says (a ratio of 1.0 = the scaling holds; under 1 = the index gives back what it moves, the shelves and ridges; over 1 = it trends), and how fat are the tails (the 99th percentile of the move in units of its own rms: a normal curve gives 2.6)?
METHOD: the index's ticks resampled to a 1 s grid (the last tick at or before each second), log moves over non-overlapping lags per hour, rms per lag, ratio = rms(lag) / (rms(1 s) x sqrt(lag)). Read-only; a lead only.
Run: SPOT=0 scripts/cloud_job.sh scripts/priskp_time_scale_check.py"""
import bisect
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
end = tape.execute("SELECT max(ts) FROM index_ticks WHERE index_id='DOGEUSD_RTI'").fetchone()[0]
lo = end - 3 * 86400
LAGS = (1, 10, 30, 60, 180, 300)
print(f"{'coin':5s} " + " ".join(f"{'rms'+str(L)+'s %':>9s}" for L in LAGS) + " | ratio to sqrt scaling at " + " ".join(f"{L}s" for L in LAGS[1:]) + " | p99/rms of 10 s, 60 s")
for ser in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M"):
    ix = index_for_series(ser)
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, end)).fetchall()
    if len(rows) < 5000:
        print(ser, "too few ticks")
        continue
    T, V = [float(r[0]) for r in rows], [float(r[1]) for r in rows]
    t0 = int(T[0]) + 1
    grid = []
    for sec in range(t0, int(T[-1])):
        k = bisect.bisect_right(T, sec) - 1
        grid.append(V[k] if sec - T[k] <= 5 else None)
    out, tails = {}, {}
    for L in LAGS:
        d = []
        for i in range(0, len(grid) - L, L):
            a, b = grid[i], grid[i + L]
            if a and b:
                d.append(math.log(b / a))
        r = math.sqrt(sum(x * x for x in d) / len(d))
        out[L] = r
        if L in (10, 60):
            q = sorted(abs(x) for x in d)
            tails[L] = q[int(0.99 * len(q))] / r
    print(f"{ser[2:-3]:5s} " + " ".join(f"{100 * out[L]:9.4f}" for L in LAGS) + " | " + " ".join(f"{out[L] / (out[1] * math.sqrt(L)):4.2f}" for L in LAGS[1:]) + f" | {tails[10]:.1f}, {tails[60]:.1f}")
