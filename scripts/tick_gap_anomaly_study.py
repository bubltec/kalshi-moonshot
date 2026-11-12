"""Per-tick curve gap and its anomalies (2026-11-12, user: "we do need to study anomalies though"; design in docs/per-tick-curve-design.md).
Question: when the curve's chance P minus the cost (ask + entry fee) clears the market's own cutoff, what do the signals that sit OUTSIDE the market's recent range pay, against the ones inside it, on the same markets? And is an anomaly a feed fault or a real dislocation?
Population: commodity 15M windows (the lane's eight series) closing since SINCE_S (default Mon 2026-11-09 05:00 UTC), spot = Kalshi's perp reference index (perpref.index_for_series, the tape's KP_* index; nothing else), every 5 s from minute 4 to 14.5, the side's ask in 0.70-0.90 (the twin's band, so only the decision differs).
Per reading: P = curvevalue.read at the 900+180 scales (pessimistic), gap = P - (ask + fee). Per (series, side) own history: the last 6 h of its readings, cutoffs recomputed hourly, at least 200 readings (earlier readings are warm-up and not scored):
  cutoff   = max(0, 80th percentile of the side's own gap)            (stated before results, not tuned)
  anomaly  = gap above the side's own 99th or below its own 1st percentile ("extreme_gap"), or the curve's long rate above 3 x its own median ("high_rate"); rule 3.
  signal   = the gap at or above the cutoff on this reading and the one 5 s before.
Feed health (from the market's own history, no fixed numbers): "troubled" when the index's ticks in the last 60 s are under half the series' own median count, or the newest tick is older than 3 x the series' own median gap between ticks.
Scored on the first signal per window and side, held to settlement, fee on the entry (ask + fee), dollars per contract and per dollar staked: BASELINE (the first in-band reading, what happened on those markets), CLEAN signals (what the rule would buy), anomalous signals (what it sits out). Then by the curve model's own shape readings at the signal (level, slope, curvature of the cushion over 5 and 13 minutes; curvemodel.features_curvature), terciles of the pooled signals (the sample's own cuts).
    scripts/cloud_job.sh scripts/tick_gap_anomaly_study.py
"""

import bisect
import calendar
import collections
import math
import os
import time as _time

from kalshi_moonshot import config, curvemodel, curvevalue, db, perpref
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 11, 9, 5, 0, 0)))))
SERIES = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M")
HIST_S, REFIT_S, MIN_HIST = 6 * 3600.0, 3600.0, 200
BAND = (0.70, 0.90)
STEP = 5
READ = [f"{k}_{m}m" for m in (5, 13) for k in ("level", "slope", "curv")]

wins = st.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
hist = collections.defaultdict(collections.deque)  # (series, side) -> (ts, gap, rate)
cache = {}  # (series, side) -> (fit ts, q01, q80, q99, median rate)
tickstat = collections.defaultdict(collections.deque)  # series -> (ts, ticks in last 60 s, newest tick age, median gap)
skipped = collections.Counter()
used = 0
readings = collections.Counter()
events = []  # dicts
t_start = _time.time()


def quant(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def fit(key, now):
    c = cache.get(key)
    if c is not None and now - c[0] < REFIT_S:
        return c
    dq = hist[key]
    while dq and dq[0][0] < now - HIST_S:
        dq.popleft()
    if len(dq) < MIN_HIST:
        return None
    gaps = [x[1] for x in dq]
    rates = [x[2] for x in dq]
    c = (now, quant(gaps, 0.01), quant(gaps, 0.80), quant(gaps, 0.99), quant(rates, 0.5))
    cache[key] = c
    return c


for w in wins:
    ser = w["ticker"].split("-")[0]
    ix = perpref.index_for_series(ser) if ser in SERIES else None
    if not ix:
        continue
    close = float(w["close_ts"])
    opened = close - 900.0
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 2720, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 600 or len(q) < 30:
        skipped["too few ticks or quotes"] += 1
        continue
    T_ = [float(x[0]) for x in tk]
    V_ = [float(x[1]) for x in tk]
    i0 = bisect.bisect_left(T_, opened - 5.0)
    if i0 >= len(T_) or T_[i0] > opened + 20.0:
        skipped["no index value at the open"] += 1
        continue
    ref = V_[i0]
    base = int(close) - 2700
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = V_[j]
            j += 1
        g.append(math.log(last) if last else None)
    o = 1800
    if any(x is None for x in g[o - 800 : o + 880]):
        skipped["gap in the index before or during the window"] += 1
        continue
    sq = [0.0]
    for a_, b_ in zip(g, g[1:]):
        sq.append(sq[-1] + (((b_ - a_) ** 2) if a_ is not None and b_ is not None else 0.0))
    qt = [float(x[0]) for x in q]
    yb, ya = [], []
    for t in range(int(close) - 900, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            yb.append(None); ya.append(None)
        else:
            yb.append(float(q[i][1])); ya.append(float(q[i][2]))
    used += 1
    streak = {"yes": 0, "no": 0}
    got = {"yes": set(), "no": set()}  # what this window's side already recorded: baseline, any, clean
    for t in range(240, 871, STEP):
        if yb[t] is None:
            continue
        now = opened + t
        i = bisect.bisect_right(T_, now) - 1
        age = now - T_[i]
        cnt = bisect.bisect_right(T_, now) - bisect.bisect_right(T_, now - 60.0)
        gaps_ = [b - a for a, b in zip(T_[max(0, i - 120) : i], T_[max(1, i - 119) : i + 1])]
        med_gap = quant(gaps_, 0.5) if gaps_ else 0.0
        for side in ("yes", "no"):
            ask = ya[t] if side == "yes" else 1.0 - yb[t]
            if not (BAND[0] <= ask <= BAND[1]):
                streak[side] = 0
                continue
            read = curvevalue.read(T_, V_, now, ref, side, 900.0 - t, "900+180")
            if read is None:
                continue
            cost = ask + fee(ask)
            gap = read.p - cost
            key = (ser, side)
            ts_dq = tickstat[ser]
            while ts_dq and ts_dq[0][0] < now - HIST_S:
                ts_dq.popleft()
            ts_dq.append((now, cnt, age, med_gap))
            c = fit(key, now)
            hist[key].append((now, gap, read.rate_long))
            if c is None:
                readings["warm-up"] += 1
                continue
            _, q01, q80, q99, med_rate = c
            cuts = max(0.0, q80)
            status = "extreme_gap" if (gap > q99 or gap < q01) else ("high_rate" if read.rate_long > 3.0 * med_rate else "clean")
            readings[status] += 1
            cnts = [x[1] for x in ts_dq]
            gps = [x[3] for x in ts_dq if x[3] > 0]
            troubled = (cnt < 0.5 * quant(cnts, 0.5)) or (bool(gps) and age > 3.0 * quant(gps, 0.5))
            sign = 1 if side == "yes" else -1
            win = 1.0 if w["result"] == side else 0.0

            def make(kind):
                shape = curvemodel.features_curvature(g, sq, o + t, math.log(ref), sign, windows=(5, 13))
                return {"kind": kind, "status": status, "troubled": troubled, "cost": cost, "net": win - cost, "gap": gap, "shape": shape, "series": ser}

            if "base" not in got[side]:
                got[side].add("base")
                events.append(make("baseline"))
            streak[side] = streak[side] + 1 if gap >= cuts else 0
            if streak[side] >= 2:
                if "any" not in got[side]:
                    got[side].add("any")
                    events.append(make("signal"))
                if status == "clean" and "clean" not in got[side]:
                    got[side].add("clean")
                    events.append(make("clean_signal"))
    if used % 200 == 0:
        print(f"  {used} windows, {len(events)} events, {_time.time() - t_start:.0f}s", flush=True)

print(f"windows scored {used} of {len(wins)} closes since {SINCE_S}; skipped {dict(skipped)}")
tot = sum(readings.values())
print(f"in-band readings: {dict(readings)} (warm-up readings are not scored)")
if tot:
    print("  share of scored readings: " + ", ".join(f"{k} {v / (tot - readings['warm-up']):.1%}" for k, v in readings.items() if k != "warm-up"))


def line(rows, label):
    n = len(rows)
    if n < 10:
        return f"    {label:46s} n {n:4d}: too few"
    cost = sum(r["cost"] for r in rows)
    net = sum(r["net"] for r in rows)
    won = sum(1 for r in rows if r["net"] > 0) / n
    return f"    {label:46s} n {n:4d} avg cost {cost / n:.2f} won {won:3.0%} | net {net:+7.2f} ({net / n * 100:+5.1f}c a bet, {net / cost * 100:+5.1f}% per $)"


base = [e for e in events if e["kind"] == "baseline"]
sig = [e for e in events if e["kind"] == "signal"]
clean = [e for e in events if e["kind"] == "clean_signal"]
print("\n=== first signal per window and side, held to settlement (dollars per contract, entry fee in the cost)")
print(line(base, "BASELINE: first in-band reading"))
print(line(clean, "CLEAN signal (what the rule would buy)"))
print(line(sig, "ANY signal (first, whatever its status)"))
for st_ in ("clean", "extreme_gap", "high_rate"):
    print(line([e for e in sig if e["status"] == st_], f"  first signal is {st_}"))
print("  -- by feed health at the signal:")
for st_ in ("clean", "extreme_gap", "high_rate"):
    for tr in (False, True):
        print(line([e for e in sig if e["status"] == st_ and e["troubled"] == tr], f"  {st_}, feed {'troubled' if tr else 'healthy'}"))
print(f"  windows-sides with an ANY signal {len(sig)}, with a CLEAN signal {len(clean)}")

print("\n=== by the curve model's shape readings at the signal (terciles of the pooled signals; clean vs anomalous)")
pool = [e for e in sig if e["shape"] is not None]
anom = [e for e in pool if e["status"] != "clean"]
cln = [e for e in pool if e["status"] == "clean"]
for ri, name in enumerate(READ):
    vals = sorted(e["shape"][ri] for e in pool)
    if len(vals) < 30:
        break
    lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    for tag, test in (("low", lambda x: x < lo), ("mid", lambda x: lo <= x < hi), ("high", lambda x: x >= hi)):
        print(f"  {name} {tag} (cuts {lo:+.2f},{hi:+.2f})")
        print(line([e for e in cln if test(e["shape"][ri])], "    clean"))
        print(line([e for e in anom if test(e["shape"][ri])], "    anomalous"))
print("DONE")
