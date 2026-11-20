"""Does a signed score from -50 to +50 (positive: the YES side looks better, negative: the NO side) sort the bets, at every point of every market? (walk-forward, all 15-minute series, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "the score can be -50 to 50, so it can represent risk to either side; a high risk on NO means YES could be a good bet, not a guarantee. Check me on this"): the model is the offset logistic of risk_score_adjust_fit.py (Kalshi's mid is the start, the cushion, time and look-back readings move it, ridge 1.0, fit on the earlier days, every other reading). The signed score is 100 x (model chance of YES - the YES mid), cut to -50..+50: points of chance the readings put above (+) or below (-) what the price already says. Zero means the price is fair by the readings. Held-out day, every reading of every series: buckets of the score; for each, the net per $1 of buying YES at its ask and of buying NO at its ask, held to the result, fee in, n and win rate. The claim to check: a positive score pays YES, a negative one pays NO, and the middle (|score| under the fee) pays neither. Bucket edges are fixed before the run. No coin or commodity is named.
Folds: train 11-17 -> test 11-18; train 11-17+11-18 -> test 11-19 (ET days). stdlib only.
Run: scripts/cloud.sh run scripts/risk_score_signed.py"""
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
    rows.append((tk, day, x, float(r["yes_bid"]), float(r["yes_ask"]), 1.0 if res[tk] == "yes" else 0.0))
print(f"{len(rows)} readings, days {sorted(set(r[1] for r in rows))}")
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
    for _ in range(8):
        g = [0.0] * len(IDX); H = [[0.0] * len(IDX) for _ in IDX]
        for _, _, x, b, a, y in data[::2]:
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
EDGES = [-50.1, -20, -10, -5, -2, 2, 5, 10, 20, 50.1]
def net(g):
    if not g: return "n 0"
    return f"n {len(g):5d} win {sum(w for _, w in g)/len(g):.2f} ask {sum(a for a, _ in g)/len(g):.2f} net {sum(((1.0 if w else 0.0) - a - fee(a)) / a for a, w in g)/len(g):+.3f}"
for train_days, test_day in ((["11-17"], "11-18"), (["11-17", "11-18"], "11-19")):
    tr = [r for r in rows if r[1] in train_days]; te = [r for r in rows if r[1] == test_day]
    w = fit(tr); sc = [(score(w, x, b, a), b, a, y) for _, _, x, b, a, y in te]
    print(f"=== train {'+'.join(train_days)} ({len(tr)}) -> test {test_day} ({len(te)} readings)")
    for lo, hi in zip(EDGES, EDGES[1:]):
        g = [(s_, b, a, y) for s_, b, a, y in sc if lo <= s_ < hi and 0.02 <= a <= 0.98 and 0.02 <= 1 - b <= 0.98]
        print(f"  score {max(lo,-50):+6.1f}..{min(hi,50):+6.1f}  BUY YES: {net([(a, y == 1.0) for _, b, a, y in g])}  |  BUY NO: {net([(1 - b, y == 0.0) for _, b, a, y in g])}")
