"""Does the risk score tell a safe 80-cent favourite from one that flips? (2026-11-18, user: "i will bet manually on like an 80+ and then it will flip in 10s"; "that's what our risk score is supposed to do")

One question on the same moments: a side priced 0.80 to 0.92 (its bid) with 90 s or more left; how often does its bid fall under 0.50 within the next 60 s (a flip), and does the risk score's chance for the side, against Kalshi's price for it, say which ones?
Moments: data/tape_archive/replay_readings.csv (scripts/risk_attribute_replay.py build): every 30 s mark of every 15-minute market since 2026-11-09, crypto series. Risk score weights refitted WALK-FORWARD (on every earlier day, used on the next), as scripts/entry_order_sim.py does; only held-out days are counted. A flip is read off the next two 30 s marks, so a fall that bounces inside them is missed.
Split: the score's chance for the side less Kalshi's mid for it, in thirds of the held-out moments (the lowest third = the score sees more risk than the price does). Nothing fitted here.
Run on the Mac: .venv/bin/python scripts/risk_score_flip_check.py > studies/results/<date>-risk-score-flip-check.txt"""
import csv
import datetime
import importlib.util
import os
from zoneinfo import ZoneInfo

import numpy as np

os.environ.setdefault("DATA_DIR", "data")
from kalshi_moonshot import riskscore, variants  # noqa: E402

_spec = importlib.util.spec_from_file_location("replay", os.path.join(os.path.dirname(__file__), "risk_attribute_replay.py"))
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)
ET = ZoneInfo("America/New_York")
rows = [r for r in list(csv.reader(open(replay.OUT)))[1:] if variants.crypto_index(r[0].split("-")[0])]
rows.sort(key=lambda r: (r[0], int(r[1])))
tick = np.array([r[0] for r in rows])
ts = np.array([int(r[1]) for r in rows])
age = np.array([float(r[2]) for r in rows])
y = np.array([float(r[3]) for r in rows])
bid = np.array([float(r[4]) for r in rows])
ask = np.array([float(r[5]) for r in rows])
X = np.array([[float(v) for v in r[6:]] for r in rows])
day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
days = sorted(set(day))
used = [i for i, n in enumerate(riskscore.NAMES) if n in riskscore.USED]
ok = (bid > 0) & (ask > 0) & (ask < 1) & (bid < 1)
at = {(a, int(b)): i for i, (a, b) in enumerate(zip(tick, ts))}
out = []  # (side bid, mid, score chance for the side, flipped, left, day)
for k in range(2, len(days)):
    tr, te = ok & np.isin(day, days[:k]), ok & (day == days[k])
    if tr.sum() < 1000 or not te.sum():
        continue
    w = replay._fit(X[tr][:, used], y[tr])
    p_yes = replay._sig(X[:, used] @ w)
    for i in np.flatnonzero(te):
        left = 900.0 - age[i]
        if left < 90.0:
            continue
        for side in (1, -1):
            b = bid[i] if side > 0 else 1.0 - ask[i]
            if not 0.80 <= b <= 0.92:
                continue
            nxt = [at.get((tick[i], int(ts[i]) + d)) for d in (30, 60)]
            fut = [(bid[j] if side > 0 else 1.0 - ask[j]) for j in nxt if j is not None and ok[j]]
            if not fut:
                continue
            mid = (bid[i] + ask[i]) / 2.0 if side > 0 else 1.0 - (bid[i] + ask[i]) / 2.0
            out.append((b, mid, p_yes[i] if side > 0 else 1.0 - p_yes[i], min(fut) < 0.50, left, days[k]))
a = np.array([(o[0], o[1], o[2], float(o[3]), o[4]) for o in out])
gap = a[:, 2] - a[:, 1]
print(f"window: held-out days {', '.join(days[2:])}; {len(out)} moments of a side with a bid of 0.80 to 0.92 and 90 s or more left; crypto series; weights fitted on earlier days only")
print(f"flips (its bid under 0.50 at one of the next two 30 s marks): {int(a[:, 3].sum())} ({100.0 * a[:, 3].mean():.1f}%) at an average price {a[:, 1].mean():.3f} (a price of {a[:, 1].mean():.2f} says about {100.0 * (1 - a[:, 1].mean()):.0f}% lose, to the end)")
cuts = np.quantile(gap, [1 / 3, 2 / 3])
for name, sel in ((f"score under the price by more than {-cuts[0] * 100:.1f} points or least over it (lowest third)", gap <= cuts[0]), ("middle third", (gap > cuts[0]) & (gap <= cuts[1])), ("score over the price the most (highest third)", gap > cuts[1])):
    s = a[sel]
    print(f"  {name:75s} n {len(s):6d} | flip rate {100.0 * s[:, 3].mean():4.1f}% | avg price {s[:, 1].mean():.3f} | avg score chance {s[:, 2].mean():.3f}")
print("--- by time left")
for lo, hi, name in ((90, 180, "1.5 to 3 min left"), (180, 420, "3 to 7 min left"), (420, 900, "over 7 min left")):
    m = (a[:, 4] >= lo) & (a[:, 4] < hi)
    g = gap[m]
    if m.sum() < 30:
        continue
    c = np.quantile(g, [1 / 3, 2 / 3])
    lows, highs = a[m][g <= c[0]], a[m][g > c[1]]
    print(f"  {name:20s} n {m.sum():6d} | flip rate {100.0 * a[m][:, 3].mean():4.1f}% | lowest third {100.0 * lows[:, 3].mean():4.1f}% | highest third {100.0 * highs[:, 3].mean():4.1f}%")
print("DONE")
