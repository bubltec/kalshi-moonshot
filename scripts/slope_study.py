"""Slope monitoring on 15-minute windows: enter on a price slope, exit when the slope turns.

Run from the project root after `history` has stored windows:  DATA_DIR=data python scripts/slope_study.py data
It tries a grid of rules on the earliest 60% of windows and reports how each did on the later 40%, beside a
control of random entries. Judge a rule by the held-out column and by how many rules were tried: the best of
54 will look good by chance. Places no orders.
"""
import itertools, random, sqlite3, statistics, sys
db = sqlite3.connect(f"{sys.argv[1]}/moonshot.db"); db.row_factory = sqlite3.Row
fee = lambda p: 0.07 * p * (1 - p)
W = []
for w in db.execute("SELECT ticker, series, close_ts, result FROM windows ORDER BY close_ts"):
    rows = db.execute("SELECT end_ts, bid_c, ask_c FROM candles WHERE ticker=? ORDER BY end_ts", (w["ticker"],)).fetchall()
    path = {}
    for r in rows:
        k = 15 - (w["close_ts"] - r["end_ts"]) // 60
        if 1 <= k <= 15 and r["bid_c"] is not None and r["ask_c"] is not None: path[k] = (r["bid_c"], r["ask_c"])
    if len(path) >= 14: W.append((w["result"], path))
split = int(len(W) * 0.6); TRAIN, TEST = W[:split], W[split:]
print(f"{len(W)} windows: {len(TRAIN)} to tune on, {len(TEST)} held out (later in time)")
def side_path(path, side):
    bid = {k: (v[0] if side == "yes" else 1 - v[1]) for k, v in path.items()}
    ask = {k: (v[1] if side == "yes" else 1 - v[0]) for k, v in path.items()}
    mid = {k: (bid[k] + ask[k]) / 2 for k in path}
    return bid, ask, mid
def run(data, L, theta, mode, exit_, lo=0.15, hi=0.85, start=3, last_entry=10, control=False, rng=None):
    pnl = []
    for res, path in data:
        for side in ("yes", "no"):
            bid, ask, mid = side_path(path, side)
            entry_k = None
            for k in range(max(start, L + 1), last_entry + 1):
                if k not in mid or (k - L) not in mid or k not in ask: continue
                slope = (mid[k] - mid[k - L]) / L
                trig = (slope >= theta) if mode == "momentum" else (slope <= -theta)
                if control: trig = rng.random() < 0.15           # same window/price band, no slope condition
                if trig and lo <= ask[k] <= hi: entry_k = k; break
            if entry_k is None: continue
            k = entry_k; cost = ask[k] + fee(ask[k]); got = None; peak = mid[k]
            for j in range(k + 1, 14):
                if j not in mid: continue
                peak = max(peak, mid[j])
                if exit_ == "slope" and (j - L) in mid and (mid[j] - mid[j - 1]) <= 0 and mid[j] < mid[j-1] and bid[j] > 0:
                    got = bid[j] - fee(bid[j]); break
                if exit_ == "trail" and mid[j] <= peak - 0.04:
                    got = bid[j] - fee(bid[j]); break
            if got is None:
                got = (1.0 if res == side else 0.0) if exit_ == "hold" else (bid[13] - fee(bid[13]) if 13 in bid and exit_ != "hold" else (1.0 if res == side else 0.0))
            pnl.append((cost, got))
    return pnl
def roi(p): return (sum(g for _, g in p) - sum(c for c, _ in p)) / sum(c for c, _ in p) if p else float("nan")
configs = list(itertools.product((2, 3, 5), (0.01, 0.02, 0.04), ("momentum", "fade"), ("hold", "slope", "trail")))
rows = []
for L, th, mode, ex in configs:
    a, b = run(TRAIN, L, th, mode, ex), run(TEST, L, th, mode, ex)
    if len(a) >= 100 and len(b) >= 60: rows.append((roi(a), len(a), roi(b), len(b), L, th, mode, ex))
rng = random.Random(5)
ctl_a = run(TRAIN, 3, 0.02, "momentum", "hold", control=True, rng=rng); ctl_b = run(TEST, 3, 0.02, "momentum", "hold", control=True, rng=rng)
print(f"\n{len(configs)} slope rules tried ({len(rows)} had enough trades). Control (random entry, same band, hold): train {roi(ctl_a):+.1%} n={len(ctl_a)} | held-out {roi(ctl_b):+.1%} n={len(ctl_b)}")
rows.sort(reverse=True)
print(f"\n{'slope window':>12} {'min slope':>10} {'idea':>9} {'exit':>6} | {'tuned-on ROI':>12} {'n':>5} | {'held-out ROI':>12} {'n':>5}")
for r in rows[:10]:
    print(f"{r[4]:>10}m {r[5]:>9.2f} {r[6]:>9} {r[7]:>6} | {r[0]:>+11.1%} {r[1]:>5} | {r[2]:>+11.1%} {r[3]:>5}")
pos_train = [r for r in rows if r[0] > 0]; pos_test = [r for r in rows if r[2] > 0]
print(f"\nPositive on the tuning windows: {len(pos_train)} of {len(rows)}; positive on the held-out windows: {len(pos_test)} of {len(rows)}")
print(f"Of the {len(pos_train)} that looked profitable when tuned, {sum(1 for r in pos_train if r[2] > 0)} stayed profitable held out.")
best_test = max(rows, key=lambda r: r[2]); print(f"Best on held-out alone (cherry-picked, so inflated): {best_test[2]:+.1%} with {best_test[3]} trades ({best_test[6]}, {best_test[4]}m, {best_test[5]}, exit {best_test[7]})")
med = statistics.median(r[2] for r in rows); print(f"Median held-out ROI across all rules: {med:+.1%}")
by = {}
for r in rows: by.setdefault((r[6]), []).append(r[2])
print("Average held-out ROI by idea: " + ", ".join(f"{k} {statistics.mean(v):+.1%}" for k, v in by.items()))
by2 = {}
for r in rows: by2.setdefault(r[7], []).append(r[2])
print("Average held-out ROI by exit:  " + ", ".join(f"{k} {statistics.mean(v):+.1%}" for k, v in by2.items()))
