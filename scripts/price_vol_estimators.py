"""Which estimate of the price's next-30-second move is closest, held out? (2026-11-18, user: "as time expires the volatility rate increases")

One question on the archived Kalshi quotes of every 15-minute market in data/tape_archive/windows.csv: at every 30 s mark of a market with 90 s or more left (and 300 s or more of quotes behind it), four estimates (kalshi_moonshot.pricevol: static = the exit's 15-minute unit as it is, windowed = the same over half the time left, implied = phi(z) x the cushion's own rate / sqrt(left), mixed = the geometric mean of the last two) are set against the realized root-mean-square one-second change of the mid over the next 30 s. Scored: mean absolute log ratio (lower is better) and the share within a factor of 1.5.
Walk-forward: each estimator is read raw (nothing fitted), and also with one scale, the median log ratio of the earlier days, applied to the next day's estimates (day 1 is only ever used to fit). Both are scored on days 2 to 8 only, so every number is held out of anything fitted. Marks where the next 30 s did not move at all (realized 0) are left out and counted. Kalshi's quotes only; a NO side moves as its YES side.
Run on the Mac, from the repo root: PYTHONPATH=src .venv/bin/python scripts/price_vol_estimators.py > studies/results/2026-11-18-price-vol-estimators.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from kalshi_moonshot import pricevol

ROOT = "data/tape_archive"
HORIZON = 30
close = {r[0]: float(r[1]) for r in csv.reader(open(f"{ROOT}/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
NAMES = list(pricevol.ESTIMATORS)
ALL = [("overall", lambda L, lead: True), ("over 7 min left", lambda L, lead: L > 420), ("3 to 7 min", lambda L, lead: 180 < L <= 420), ("1.5 to 3 min", lambda L, lead: L <= 180),
       ("near even (lead < 0.80)", lambda L, lead: lead < 0.80), ("favourite (0.80 to 0.92)", lambda L, lead: 0.80 <= lead <= 0.92)]


def marks_of_day(f):
    """Rows (left, lead, realized, {name: estimate}) for every mark of the day's markets."""
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-")).to_pandas().sort_values(["ticker", "ts"])
    out, zero = [], 0
    for ticker, g in t.groupby("ticker", sort=False):
        if ticker not in close:
            continue
        g = g[(g["bid"] > 0) & (g["ask"] > 0) & (g["ask"] <= 1) & (g["ask"] >= g["bid"])]
        if len(g) < 30:
            continue
        sec = np.floor(g["ts"].to_numpy()).astype(np.int64)
        mid = pd.Series(((g["bid"] + g["ask"]) / 2.0).to_numpy(), index=sec).groupby(level=0).last()
        s0, s1 = int(mid.index[0]), int(min(mid.index[-1], close[ticker]))
        if s1 - s0 < 400:
            continue
        full = mid.reindex(np.arange(s0, s1 + 1))
        seen = full.notna().to_numpy()
        lastq = np.maximum.accumulate(np.where(seen, np.arange(len(full)), -10**9))
        m = full.ffill().to_numpy()
        for L in range(90, 601, 30):
            i = int(close[ticker]) - L - s0
            if i < pricevol.MIN_HISTORY or i + HORIZON >= len(m) or i - lastq[i] > 10:
                continue
            d = np.diff(m[i : i + HORIZON + 1])
            real = math.sqrt(float(np.mean(d * d)))
            if real <= 0:
                zero += 1
                continue
            seq = m[max(0, i - pricevol.SPAN) : i + 1]
            est = {k: fn(seq, float(L)) for k, fn in pricevol.ESTIMATORS.items()}
            if any(v is None for v in est.values()):
                continue
            out.append((L, max(m[i], 1 - m[i]), real, est))
    return out, zero


days = sorted(glob.glob(f"{ROOT}/quotes/day=*/part-0.parquet"))
per_day, zeros = {}, 0
for f in days:
    rows, z = marks_of_day(f)
    per_day[f.split("day=")[1][:10]] = rows
    zeros += z
names = sorted(per_day)
held = names[1:]
sq = {}
logs = {}  # (name, calibrated) -> list of (L, lead, log ratio)
for k, day in enumerate(held, start=1):
    for nm in NAMES:
        prior = [math.log(e[nm] / r) for d in names[:k] for (_, _, r, e) in per_day[d]]
        shift = float(np.median(prior))  # fitted on earlier days only
        for L, lead, r, e in per_day[day]:
            raw = math.log(e[nm] / r)
            logs.setdefault((nm, False), []).append((L, lead, raw))
            sq.setdefault(nm, []).append((L, lead, r * r, e[nm] ** 2))
            logs.setdefault((nm, True), []).append((L, lead, raw - shift))
n = len(logs[(NAMES[0], False)])
print(f"window: {names[0]} to {names[-1]}; scored on {held[0]} to {held[-1]} (held out; {names[0]} only fits); {n:,d} marks (every 30 s with 90 to 600 s left, 300 s or more of quotes), {zeros:,d} marks left out because the next 30 s did not move")
print("score: mean absolute log ratio of the estimate to the realized one-second rms of the next 30 s (lower is better); within = share of marks within a factor of 1.5; bias = mean log ratio (negative = too low)")
for calibrated in (False, True):
    print(f"\n{'one scale fitted on earlier days' if calibrated else 'raw, nothing fitted'}")
    print(f"  {'split':26s} {'n':>8s} | " + " | ".join(f"{nm:^26s}" for nm in NAMES))
    for label, keep in ALL:
        cells, cnt = [], 0
        for nm in NAMES:
            v = np.array([x for L, lead, x in logs[(nm, calibrated)] if keep(L, lead)])
            cnt = len(v)
            cells.append(f"mal {np.mean(np.abs(v)):.3f} in1.5 {100 * np.mean(np.abs(v) <= math.log(1.5)):4.1f}% b{np.mean(v):+.2f}" if cnt else "-")
        print(f"  {label:26s} {cnt:8,d} | " + " | ".join(f"{c:^26s}" for c in cells))
print("\nin the root-mean-square sense (what a tolerance in a typical-minute unit is paid against): realized rms / estimated rms over all marks of the split, 1.00 = right, over 1 = the estimate is too small")
print(f"  {'split':26s} {'n':>8s} | " + " | ".join(f"{nm:^10s}" for nm in NAMES))
for label, keep in ALL:
    cells = []
    for nm in NAMES:
        v = [(a, b) for L, lead, a, b in sq[nm] if keep(L, lead)]
        cells.append(f"{math.sqrt(sum(a for a, _ in v) / sum(b for _, b in v)):^10.2f}")
    print(f"  {label:26s} {len(v):8,d} | " + " | ".join(cells))
print("DONE")
