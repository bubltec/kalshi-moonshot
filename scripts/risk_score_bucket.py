"""Is a 1-100 risk score, shown NEXT TO Kalshi's price (not added to it, not a price), ordered with what happened at that price? (walk-forward, crypto 15-minute markets, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "it's not a price itself, it's a score of 1-100", adjacent to the price): the weights come from the offset fit of risk_score_adjust_fit.py (cushion/time/look-backs, ridge 1.0, fit on the earlier days). A side's raw score is its weighted sum of readings (sign flipped for NO); the 1-100 score is that raw number's percentile among the earlier days' sides in the same price band (0.60-0.85, spread <= 0.03). On the held-out day, for every side at its first in-band reading per market, bucket by score (1-20 ... 81-100) and report n, win rate, mean ask and net per $1 held to the result, fee in. A useful score has net rising with the score inside the same price band. Buckets are fixed fifths set before the run. No coin is named.
Run: scripts/cloud.sh run scripts/risk_score_bucket.py"""
import collections, datetime, json, math, os
from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
N = list(riskscore.NAMES)
SETS = {
    "B live readings": [n for n in riskscore.USED if n != "price"],
    "C cushion+time+look-backs": ["cushion", "time", "t15", "t30", "t60", "nudge", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far"],
    "D all fourteen": [n for n in N if n != "price"],
}
rows = []
for r in st.execute("SELECT ticker, ts, x, yes_bid, yes_ask FROM risk_readings ORDER BY ts").fetchall():
    tk = r["ticker"]
    if not tk.startswith(CRYPTO) or res.get(tk) not in ("yes", "no") or int(r["ts"]) % 60 >= 30: continue
    x = json.loads(r["x"]) if isinstance(r["x"], str) else list(r["x"])
    if len(x) != len(N) or r["yes_bid"] is None or r["yes_ask"] is None: continue
    day = datetime.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d")
    rows.append((tk, day, x, float(r["yes_bid"]), float(r["yes_ask"]), 1.0 if res[tk] == "yes" else 0.0))
print(f"{len(rows)} readings, days {sorted(set(r[1] for r in rows))}")
def mid(b, a): return min(0.99, max(0.01, (b + a) / 2))
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
def lg(p): return math.log(p / (1 - p))
def fit(data, idx):
    w = [0.0] * len(idx)
    for _ in range(8):
        g = [0.0] * len(idx); H = [[0.0] * len(idx) for _ in idx]
        for _, _, x, b0, a0, y in data:
            v = [x[i] for i in idx]; z = max(-30, min(30, lg(mid(b0, a0)) + sum(a * b for a, b in zip(w, v)))); p = 1 / (1 + math.exp(-z)); q = p * (1 - p) + 1e-9
            for i in range(len(idx)):
                g[i] += (p - y) * v[i]
                for j in range(i + 1): H[i][j] += q * v[i] * v[j]
        for i in range(len(idx)):
            H[i][i] += 1.0; g[i] += 1.0 * w[i]
            for j in range(i): H[j][i] = H[i][j]
        step = solve(H, g); w = [a - b for a, b in zip(w, step)]
    return w
def chance(w, idx, x, b, a):
    return 1 / (1 + math.exp(-max(-30, min(30, lg(mid(b, a)) + sum(a_ * x[i] for a_, i in zip(w, idx))))))

def raw(w, idx, x, side): 
    v = sum(a * x[i] for a, i in zip(w, idx)); return v if side == "yes" else -v
def sides(data, w, idx):
    seen = set(); out = []
    for tk, _, x, b, a, y in data:
        for side, ask, win in (("yes", a, y == 1.0), ("no", 1 - b, y == 0.0)):
            if (tk, side) in seen or not 0.60 <= ask <= 0.85 or a - b > 0.03: continue
            seen.add((tk, side)); out.append((raw(w, idx, x, side), ask, win))
    return out
for train_days, test_day in ((["11-17"], "11-18"), (["11-17", "11-18"], "11-19")):
    tr = [r for r in rows if r[1] in train_days]; te = [r for r in rows if r[1] == test_day]
    print(f"=== train {'+'.join(train_days)} -> test {test_day}")
    for name, names in SETS.items():
        idx = [N.index(n) for n in names]; w = fit(tr, idx)
        ref = sorted(r for r, _, _ in sides(tr, w, idx))
        def score(r): 
            import bisect; return max(1, min(100, 1 + int(100 * bisect.bisect_left(ref, r) / len(ref))))
        got = sides(te, w, idx); rowsb = []
        for lo in (1, 21, 41, 61, 81):
            g = [(a, win) for r, a, win in got if lo <= score(r) < lo + 20 or (lo == 81 and score(r) == 100)]
            if not g: rowsb.append(f"{lo}-{lo+19}: n 0"); continue
            net = sum(((1.0 if win else 0.0) - a - fee(a)) / a for a, win in g) / len(g)
            rowsb.append(f"{lo}-{lo+19}: n {len(g)} win {sum(w_ for _, w_ in g)/len(g):.2f} ask {sum(a for a, _ in g)/len(g):.2f} net {net:+.3f}")
        print(f"  {name:30s} " + " | ".join(rowsb))
