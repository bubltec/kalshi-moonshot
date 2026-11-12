"""What does each curve feature do on its own? (2026-11-12, user: "not even curve predictions, just curve features"; features on the curve that help us understand time frames and cushion)
No model, no bar, no picks. Every 30-second moment of every settled 15-minute market since Monday 2026-11-09 (crypto and commodity series the lanes trade), each side, with the twelve curve readings at four windows: level (the cushion on the bet's side now, smoothed), slope (its change over the window) and curvature (its bend), at 5, 13, 34 and 180 minutes (the 3-hour window is the user's addition, curvemodel.LONG_WINDOWS). Each reading is in that window's own typical move, so no asset enters.
For each reading, terciles of the moments themselves; per bucket: moments, markets, how often that side won, the mean ask (what Kalshi charged for it, shown for context, because a feature that lines up with a high ask is only repeating the price), and the mean net per $1 bought at that ask held to settlement with the entry fee.
Then the two pairs we have already seen matter, as 3x3 tables: the 13-minute level against the 13-minute slope, and the 5-minute level against the 180-minute level (does a long time frame change what a short cushion means).
Two sets of moments: ALL (ask 0.05-0.95, minutes 1-14) and BAND (ask 0.70-0.90, minutes 7-15, the live lane's own). Moments from one market are not independent, so the markets column is the honest count. Nothing is chosen after the results; no halves.
    scripts/cloud_job.sh scripts/curve_features_by_bucket.py
"""
import dataclasses, datetime as dt, time
from kalshi_moonshot import config, curvemodel, lanes
from kalshi_moonshot.curvemodel import KINDS

s = config.reload()
ET = dt.timezone(dt.timedelta(hours=-5))
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
curvemodel.TRAIN_DAYS = (time.time() - MON) / 86400.0 + 0.1
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
orig = lanes.lane_specs


def wide(st):
    return {k: (dataclasses.replace(v, price=(0.05, 0.95), entry_elapsed=(1.0, 14.5), hours=(0.0, 0.26)) if v.model_buy else v) for k, v in orig(st).items()}


lanes.lane_specs = wide
meta = []
rows = curvemodel.build_rows(s, kind="curvature3", duty=1.0, meta=meta)
lanes.lane_specs = orig
M = [(r, m) for r, m in zip(rows, meta) if m[0] > MON + 60]
names = KINDS["curvature3"]
print(f"{len(M)} moments since Monday, {len({m[2] for _, m in M})} markets; the tape ends {hm(max(m[0] for _, m in M))} ET; moments need 180 minutes of earlier history, so the first three hours of each series' tape are not in")
SETS = {"ALL (ask 0.05-0.95, minutes 1-14)": lambda ask, e: True, "BAND (ask 0.70-0.90, minutes 7-15)": lambda ask, e: 0.70 <= ask <= 0.90 and e >= 420}


def stat(label, xs):
    if not xs:
        return f"   {label:12s} n=0"
    n = len(xs)
    return f"   {label:12s} moments {n:6d} | markets {len({m[2] for _, m in xs}):4d} | won {sum(1 for (r, m) in xs if r[2] > 0) / n:5.1%} | mean ask {sum(r[1] for r, _ in xs) / n:.3f} | net per $1 {sum(r[2] for r, _ in xs) / n:+.3f}"


for sname, keep in SETS.items():
    S = [(r, m) for r, m in M if keep(r[1], m[1])]
    print(f"\n=== {sname}: {len(S)} moments ===")
    for j, nm in enumerate(names):
        v = sorted(r[0][j] for r, _ in S)
        if len(v) < 90:
            continue
        c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
        print(f" {nm} (cuts {c1:+.2f}, {c2:+.2f})")
        print(stat("low", [x for x in S if x[0][0][j] < c1]))
        print(stat("mid", [x for x in S if c1 <= x[0][0][j] < c2]))
        print(stat("high", [x for x in S if x[0][0][j] >= c2]))
    for (ia, ib) in ((names.index("level_13m"), names.index("slope_13m")), (names.index("level_5m"), names.index("level_180m"))):
        va, vb = sorted(r[0][ia] for r, _ in S), sorted(r[0][ib] for r, _ in S)
        a1, a2, b1, b2 = va[len(va) // 3], va[2 * len(va) // 3], vb[len(vb) // 3], vb[2 * len(vb) // 3]
        tier = lambda x, c1, c2: 0 if x < c1 else 1 if x < c2 else 2  # noqa: E731
        print(f" {names[ia]} (rows) against {names[ib]} (columns), terciles low/mid/high; each cell: markets, won, net per $1")
        for ta in range(3):
            cells = []
            for tb in range(3):
                xs = [x for x in S if tier(x[0][0][ia], a1, a2) == ta and tier(x[0][0][ib], b1, b2) == tb]
                cells.append(f"{len({m[2] for _, m in xs}):4d} {sum(1 for r, _ in xs if r[2] > 0) / max(1, len(xs)):4.0%} {sum(r[2] for r, _ in xs) / max(1, len(xs)):+.2f}")
            print(f"   {['low', 'mid', 'high'][ta]:5s} | " + "   |   ".join(cells))
print("DONE")
