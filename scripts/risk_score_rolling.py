"""Does the signed score hold when the model is refit on the market's own recent hours, so it can flip with the day? (rolling, all 15-minute series, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "the sign should flip between days; the markets shift over time"): the fit of risk_score_paired.py used whole earlier days and its sign failed to carry. Here the offset logistic (Kalshi's mid + cushion, time, look-backs; ridge 1.0) is refit every hour on the trailing W hours only (W = 1, 3, 6, 12, set before the run) and scores the next hour, so the weights follow the market. Out of sample, every reading of every series: the signed score (100 x (chance of YES - YES mid), cut to +-50) in buckets; per bucket the YES-minus-NO pnl per contract (cents) less that test hour's own mean of the same, so the hour's drift drops out; and the rule "buy YES when the score is +5 or more, NO when -5 or less" (net per $1 held to the result, fee in). A window whose buckets rise with the score and whose rule nets positive is the one the score should read. The +-5 and the buckets are fixed before the run. No coin or commodity is named.
Run: scripts/cloud.sh run scripts/risk_score_rolling.py"""
import datetime, json, math, os
from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
N = list(riskscore.NAMES)
IDX = [N.index(n) for n in ["cushion", "time", "t15", "t30", "t60", "nudge", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far"]]
rows = []
for r in st.execute("SELECT ticker, ts, x, yes_bid, yes_ask FROM risk_readings ORDER BY ts").fetchall():
    tk = r["ticker"]
    if res.get(tk) not in ("yes", "no"): continue
    x = json.loads(r["x"]) if isinstance(r["x"], str) else list(r["x"])
    if len(x) != len(N) or r["yes_bid"] is None or r["yes_ask"] is None: continue
    day = datetime.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d")
    rows.append((tk, float(r["ts"]), x, float(r["yes_bid"]), float(r["yes_ask"]), 1.0 if res[tk] == "yes" else 0.0))
print(f"{len(rows)} readings")
def mid(b, a): return min(0.99, max(0.01, (b + a) / 2))
def lg(p): return math.log(p / (1 - p))
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
def fit(data):
    w = [0.0] * len(IDX)
    for _ in range(6):
        g = [0.0] * len(IDX); H = [[0.0] * len(IDX) for _ in IDX]
        for _, _, x, b, a, y in data[::3]:
            v = [x[i] for i in IDX]; z = max(-30, min(30, lg(mid(b, a)) + sum(p * q for p, q in zip(w, v)))); p = 1 / (1 + math.exp(-z)); q = p * (1 - p) + 1e-9
            for i in range(len(IDX)):
                g[i] += (p - y) * v[i]
                for j in range(i + 1): H[i][j] += q * v[i] * v[j]
        for i in range(len(IDX)):
            H[i][i] += 1.0; g[i] += 1.0 * w[i]
            for j in range(i): H[j][i] = H[i][j]
        w = [p - q for p, q in zip(w, solve(H, g))]
    return w
def score(w, x, b, a):
    z = lg(mid(b, a)) + sum(p * x[i] for p, i in zip(w, IDX)); p = 1 / (1 + math.exp(-max(-30, min(30, z))))
    return max(-50.0, min(50.0, 100 * (p - mid(b, a))))
EDGES = [-50.1, -10, -5, -2, 2, 5, 10, 50.1]
def pnl(b, a, y): return (y - a - fee(a)) - ((1.0 - y) - (1 - b) - fee(1 - b))
rows.sort(key=lambda r: r[1])
t0, t1 = rows[0][1], rows[-1][1]
for W in (1, 3, 6, 12):
    acc = {k: [] for k in range(len(EDGES) - 1)}; buys = []; nh = 0
    t = t0 + 12 * 3600
    while t + 3600 <= t1:
        tr = [r for r in rows if t - W * 3600 <= r[1] < t]; te = [r for r in rows if t <= r[1] < t + 3600 and 0.02 <= r[4] <= 0.98 and 0.02 <= 1 - r[3] <= 0.98]
        t += 3600
        if len(tr) < 400 or len(te) < 200: continue
        w = fit(tr); nh += 1
        sc = [(score(w, x, b, a), pnl(b, a, y), b, a, y) for _, _, x, b, a, y in te]
        base = sum(p for _, p, _, _, _ in sc) / len(sc)
        for s_, p, b, a, y in sc:
            for k in range(len(EDGES) - 1):
                if EDGES[k] <= s_ < EDGES[k + 1]: acc[k].append(p - base)
            if s_ >= 5: buys.append((a, y == 1.0))
            elif s_ <= -5: buys.append((1 - b, y == 0.0))
    print(f"=== trailing {W} h, refit hourly, {nh} test hours")
    for k in range(len(EDGES) - 1):
        g = acc[k]
        if g: print(f"  score {max(EDGES[k],-50):+6.1f}..{min(EDGES[k+1],50):+6.1f}  n {len(g):6d}  YES minus NO vs the hour {100*sum(g)/len(g):+6.1f}c")
    if buys: print(f"  rule +-5: n {len(buys)} win {sum(w for _, w in buys)/len(buys):.2f} net {sum(((1.0 if w else 0.0) - a - fee(a)) / a for a, w in buys)/len(buys):+.3f}")
