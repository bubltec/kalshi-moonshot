"""(Extra: the coin-flip moments by age, user 2026-11-19: "a bet at 0 timer and 50/50 isn't a good bet".) A 1-100 take-the-bet score read at EVERY point of EVERY market, at every price, not in a lane's band: does it order the bets by what they paid, and does the price alone? (walk-forward, all 15-minute series, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "you're using the lane's pricing; it needs to follow the curves in every market and evaluate every point"): the first version (risk_score_take_bet.py) kept a lane's view: crypto only, asks 0.60-0.85, spread <= 0.03, every other reading, the first reading per market side. This one keeps every recorded reading (about every 30 s, whole market, first minute and edges included) of every series the recorder reads, both sides, any ask from 0.02 to 0.98, any spread (the ask is what a buy pays). Readings are turned to the side's view (the YES readings that carry a direction change sign for NO; the previous-markets flip count does not); a logistic on "the side paid" is fit on the earlier days (every other reading, to keep it light); the score is the percentile (1-100), among the earlier days' sides, of the expected net per $1 (chance - ask - fee) / ask. Sets: P price alone (value), C value + cushion + time + every look-back, D all fifteen. Held-out day: every reading, bucketed by score in tenths; each reports n, win rate, mean ask, net per $1 held to the result, fee in, and net per contract in cents. Then the same split by the ask's third (under 0.35, 0.35-0.65, over 0.65) so a score that only sorts by price shows. Tenths and thirds are fixed before the run. No coin or commodity is named; the series are not split.
Folds: train 11-17 -> test 11-18; train 11-17+11-18 -> test 11-19 (ET days). stdlib only.
Run: scripts/cloud.sh run scripts/risk_score_every_point.py"""
import bisect, datetime, json, math, os
from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
N = list(riskscore.NAMES)
UNSIGNED = {N.index("flips")}  # the previous markets' flip count has no direction
SETS = {"P price alone": ["price"], "C value+cushion+time+look-backs": ["price", "cushion", "time", "t15", "t30", "t60", "nudge", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far"], "D all fifteen": N}
sides = []  # (ticker, day, side, oriented x, ask, win)
for r in st.execute("SELECT ticker, ts, x, yes_bid, yes_ask, age FROM risk_readings ORDER BY ts").fetchall():
    tk = r["ticker"]
    if res.get(tk) not in ("yes", "no"): continue
    x = json.loads(r["x"]) if isinstance(r["x"], str) else list(r["x"])
    if len(x) != len(N) or r["yes_bid"] is None or r["yes_ask"] is None: continue
    b, a = float(r["yes_bid"]), float(r["yes_ask"])
    day = datetime.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d")
    for side, ask in (("yes", a), ("no", 1 - b)):
        if not 0.02 <= ask <= 0.98: continue
        sg = 1.0 if side == "yes" else -1.0
        sides.append((tk, day, side, [v if i in UNSIGNED else sg * v for i, v in enumerate(x)], ask, 1.0 if res[tk] == side else 0.0, float(r["age"] or 0)))
print(f"{len(sides)} side readings, days {sorted(set(r[1] for r in sides))}")
def solve(A, b):
    n = len(b); M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(M[k][i])); M[i], M[p] = M[p], M[i]
        d = M[i][i] or 1e-12
        for j in range(i, n + 1): M[i][j] /= d
        for k in range(n):
            if k != i:
                f = M[k][i]
                for j in range(i, n + 1): M[k][j] -= f * M[i][j]
    return [M[i][n] for i in range(n)]
def fit(data, idx):
    w = [0.0] * len(idx)
    for _ in range(8):
        g = [0.0] * len(idx); H = [[0.0] * len(idx) for _ in idx]
        for _, _, _, x, _, y, _ in data[::2]:
            v = [x[i] for i in idx]; z = max(-30, min(30, sum(a * b for a, b in zip(w, v)))); p = 1 / (1 + math.exp(-z)); q = p * (1 - p) + 1e-9
            for i in range(len(idx)):
                g[i] += (p - y) * v[i]
                for j in range(i + 1): H[i][j] += q * v[i] * v[j]
        for i in range(len(idx)):
            H[i][i] += 1.0; g[i] += 1.0 * w[i]
            for j in range(i): H[j][i] = H[i][j]
        w = [a - b for a, b in zip(w, solve(H, g))]
    return w
def value(w, idx, x, ask):
    p = 1 / (1 + math.exp(-max(-30, min(30, sum(a * x[i] for a, i in zip(w, idx))))))
    return (p - ask - fee(ask)) / ask
def fmt(g):
    if not g: return "n 0"
    net = sum(((1.0 if win else 0.0) - a - fee(a)) / a for a, win in g) / len(g)
    cents = 100 * sum((1.0 if win else 0.0) - a - fee(a) for a, win in g) / len(g)
    return f"n {len(g)} win {sum(w_ for _, w_ in g)/len(g):.2f} ask {sum(a for a, _ in g)/len(g):.2f} net {net:+.3f} ({cents:+.1f}c)"
for train_days, test_day in ((["11-17"], "11-18"), (["11-17", "11-18"], "11-19")):
    tr = [r for r in sides if r[1] in train_days]; te = [r for r in sides if r[1] == test_day]
    print(f"=== train {'+'.join(train_days)} ({len(tr)}) -> test {test_day} ({len(te)} side readings, {len(set(r[0] for r in te))} markets)")
    for name, names in SETS.items():
        idx = [N.index(n) for n in names]; w = fit(tr, idx)
        ref = sorted(value(w, idx, x, a) for _, _, _, x, a, _, _ in tr)
        scored = [(max(1, min(100, 1 + int(100 * bisect.bisect_left(ref, value(w, idx, x, a)) / len(ref)))), a, win, age) for _, _, _, x, a, win, age in te]
        print(f"  {name}: asks 0.45-0.55 by age (seconds into the 15 minutes)")
        for alo, ahi in ((0, 120), (120, 450), (450, 780), (780, 901)):
            g = [(sc, a, win) for sc, a, win, age in scored if 0.45 <= a <= 0.55 and alo <= age < ahi]
            mean_sc = sum(sc for sc, _, _ in g) / len(g) if g else 0
            print(f"    age {alo:3d}-{ahi:3d}: mean score {mean_sc:5.1f} | " + fmt([(a, win) for _, a, win in g]))
