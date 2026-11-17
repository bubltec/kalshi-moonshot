"""Does the spot move before the contract's price does?

Question (user, 2026-11-17, on the curve sale: "lets fix it"): the curve sale sells when the bid is over the curve's own chance. On the tape a standing gap of that kind means the curve is wrong, not the market (studies/results/2026-11-17-curve-sale-settings.txt). The one way such a sale could be right is speed: the index moves against the bet and the contract's bid has not followed yet. Is there such a lag, and is it larger than the cost of selling?
One question on the same moments: every second of every crypto 15-minute market on the archive (Kalshi's own settlement index against that market's quotes), from one minute in to 40 s before the close, with the YES mid between 0.15 and 0.85. The index's move over the last 5 s is put in its own typical 5 s moves for that market; then what the YES mid did over the same 5 s, and over the next 5, 10 and 30 s, in cents, counted in the direction of the index's move.
Kalshi's feed only (the index is Kalshi's). Settings fixed before the run.

    .venv/bin/python scripts/spot_leads_quote_study.py
"""
import csv
import glob
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import risk_attribute_replay as R  # noqa: E402

from kalshi_moonshot import variants  # noqa: E402

wins = {r[0]: float(r[1]) for r in csv.reader(open(f"{R.ROOT}/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
quotes = R._quotes_15m()
series = sorted({t.split("-")[0] for t in wins})
index_of = {s: variants.crypto_index(s) for s in series}
need = {v for v in index_of.values() if v}
parts = defaultdict(list)
for f in sorted(glob.glob(f"{R.ROOT}/index_ticks/day=*/part-0.parquet")):
    t = pq.read_table(f, columns=["ts", "index_id", "value"])
    ids, ts, vs = np.array(t["index_id"].to_pylist()), t["ts"].to_numpy(), t["value"].to_numpy()
    for i in need:
        m = (ids == i) & (vs > 0)
        parts[i].append((ts[m], vs[m]))
index = {}
for i, ps in parts.items():
    ts, vs = np.concatenate([p[0] for p in ps]), np.concatenate([p[1] for p in ps])
    o = np.argsort(ts, kind="stable")
    index[i] = (ts[o], vs[o])

Z, SAME, N5, N10, N30, SPREAD = [], [], [], [], [], []
markets = 0
for tk, close in wins.items():
    ix = index_of.get(tk.split("-")[0])
    if not ix or ix not in index or tk not in quotes:
        continue
    its, ivs = index[ix]
    qt, qb, qa = quotes[tk]
    grid = np.arange(close - 900.0, close + 1.0, 1.0)
    si = np.searchsorted(its, grid, side="right") - 1
    qi = np.searchsorted(qt, grid, side="right") - 1
    if si.min() < 0 or (grid - its[si]).max() > 5.0 or qi[60] < 0:
        continue
    ok_q = qi >= 0
    spot = np.log(ivs[si])
    mid = np.where(ok_q, (qb[np.clip(qi, 0, None)] + qa[np.clip(qi, 0, None)]) / 2.0, np.nan)
    spr = np.where(ok_q, qa[np.clip(qi, 0, None)] - qb[np.clip(qi, 0, None)], np.nan)
    d5 = spot[5:] - spot[:-5]
    u5 = np.sqrt(np.mean(d5 ** 2))
    if not u5 > 0:
        continue
    t = np.arange(60, 860)
    z = (spot[t] - spot[t - 5]) / u5
    sgn = np.sign(z)
    keep = (mid[t] > 0.15) & (mid[t] < 0.85) & np.isfinite(mid[t - 5]) & np.isfinite(mid[t + 30])
    Z.append(z[keep]); SAME.append((sgn * (mid[t] - mid[t - 5]))[keep]); N5.append((sgn * (mid[t + 5] - mid[t]))[keep]); N10.append((sgn * (mid[t + 10] - mid[t]))[keep]); N30.append((sgn * (mid[t + 30] - mid[t]))[keep]); SPREAD.append(spr[t][keep])
    markets += 1
Z, SAME, N5, N10, N30, SPREAD = (np.concatenate(a) for a in (Z, SAME, N5, N10, N30, SPREAD))
print(f"window: 2026-11-09 to 11-16, {markets} crypto 15-minute markets, {len(Z)} seconds with the YES mid between 0.15 and 0.85. A typical 5 s index move is 1 unit. The spread (ask less bid) at those seconds: a median {100 * np.nanmedian(SPREAD):.1f} cents.")
print("cents the YES mid moved, counted in the direction the index had just moved (+ = the price followed the index)\n")
print(f"{'the index over the last 5 s':34s} {'seconds':>9s} {'the mid, same 5 s':>18s} {'next 5 s':>9s} {'next 10 s':>10s} {'next 30 s':>10s}")
for lab, lo, hi in (("under half a unit", 0, 0.5), ("0.5 to 1", 0.5, 1), ("1 to 2", 1, 2), ("2 to 3", 2, 3), ("3 and over (a sharp move)", 3, 99)):
    m = (np.abs(Z) >= lo) & (np.abs(Z) < hi)
    print(f"{lab:34s} {int(m.sum()):9d} {100 * SAME[m].mean():+18.2f} {100 * N5[m].mean():+9.2f} {100 * N10[m].mean():+10.2f} {100 * N30[m].mean():+10.2f}")
print("\nonly the seconds where the price had NOT yet followed (the mid flat or the other way over the same 5 s): is there a catch-up to sell ahead of?")
print(f"{'the index over the last 5 s':34s} {'seconds':>9s} {'share of all':>13s} {'next 5 s':>9s} {'next 10 s':>10s} {'next 30 s':>10s}")
for lab, lo, hi in (("1 to 2", 1, 2), ("2 to 3", 2, 3), ("3 and over (a sharp move)", 3, 99)):
    a = (np.abs(Z) >= lo) & (np.abs(Z) < hi)
    m = a & (SAME <= 0)
    print(f"{lab:34s} {int(m.sum()):9d} {100 * m.sum() / max(a.sum(), 1):12.0f}% {100 * N5[m].mean():+9.2f} {100 * N10[m].mean():+10.2f} {100 * N30[m].mean():+10.2f}")
