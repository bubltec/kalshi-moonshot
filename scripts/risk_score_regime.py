"""(With the recent-outcomes reading, user 2026-11-19: the sign flipped between days because the day's drift moved the whole market; regime.py's idea. Two extra readings, from windows settled BEFORE each reading: the share that resolved YES over the last 1 h and 3 h of closes, minus 0.5 (0 when fewer than 20 settled; past only).) A 0-to-1 score of YES (0 a sure NO, 0.5 a toss-up, 1 a sure YES) read from the spot's own curve with NO price in it: does it say something the market's price does not? (rolling hourly refit, all 15-minute series, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "change the model ... the 0-1 idea, .5 being 50/50, 0 a definite No, 1 a definite yes"): the signed -50..+50 version started from Kalshi's mid, so it could only echo the price; its sign did not carry (be999a2). This one leaves the price out: logistic on the YES result from the cushion, the strike's place in the last 15/30/60 minutes, the spot's travel over 3 and 15 minutes (and 5, by cushion zone), the previous markets' margin, and a constant; refit every hour on the trailing W hours (W = 3, 6, 12, set before the run), ridge 1.0, scoring the next hour. NO is 1 minus the score, so one number serves both sides. Three reports, out of sample, every reading of every series: (1) the score in tenths: its mean, the YES rate, Kalshi's mean mid (does 0.9 mean 90%, and does the price already say it); (2) log loss of the score and of the mid; (3) the rule "buy the side whose chance (score for YES, 1 - score for NO) beats its ask plus the fee by at least a gap" for gaps 0, 0.03, 0.05, 0.08 (one buy per market side, the first such reading): n, win rate, net per $1 held to the result, fee in. Tenths and gaps are fixed before the run. No coin or commodity is named.
Run: scripts/cloud_job.sh scripts/risk_score_regime.py"""
import datetime, json, math, os
from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
win_close = sorted((float(r["close_ts"]), 1 if r["result"] == "yes" else 0) for r in st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL").fetchall())
import bisect as _b
_ct = [c for c, _ in win_close]; _cum = [0]
for _, y_ in win_close: _cum.append(_cum[-1] + y_)
def recent_yes(t, hours):
    lo = _b.bisect_left(_ct, t - hours * 3600); hi = _b.bisect_left(_ct, t)
    return (_cum[hi] - _cum[lo]) / (hi - lo) - 0.5 if hi - lo >= 20 else 0.0
N = list(riskscore.NAMES)
IDX = [N.index(n) for n in ["cushion", "t15", "t30", "t60", "margin", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far"]] + [len(N), len(N) + 1]  # the spot's own readings: no price, no mid-based reading (flip rate, time x price, nudge)
rows = []
for r in st.execute("SELECT ticker, ts, x, yes_bid, yes_ask FROM risk_readings ORDER BY ts").fetchall():
    tk = r["ticker"]
    if res.get(tk) not in ("yes", "no"): continue
    x = json.loads(r["x"]) if isinstance(r["x"], str) else list(r["x"])
    if len(x) != len(N) or r["yes_bid"] is None or r["yes_ask"] is None: continue
    day = datetime.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d")
    x = x + [recent_yes(float(r["ts"]), 1), recent_yes(float(r["ts"]), 3)]
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
    w = [0.0] * (len(IDX) + 1)
    for _ in range(6):
        K = len(IDX) + 1; g = [0.0] * K; H = [[0.0] * K for _ in range(K)]
        for _, _, x, b, a, y in data[::3]:
            v = [x[i] for i in IDX] + [1.0]; z = max(-30, min(30, sum(p * q for p, q in zip(w, v)))); p = 1 / (1 + math.exp(-z)); q = p * (1 - p) + 1e-9
            for i in range(K):
                g[i] += (p - y) * v[i]
                for j in range(i + 1): H[i][j] += q * v[i] * v[j]
        for i in range(K):
            H[i][i] += 1.0; g[i] += 1.0 * w[i]
            for j in range(i): H[j][i] = H[i][j]
        w = [p - q for p, q in zip(w, solve(H, g))]
    return w
def score(w, x, b, a):
    z = sum(p * x[i] for p, i in zip(w, IDX)) + w[-1]; return 1 / (1 + math.exp(-max(-30, min(30, z))))
rows.sort(key=lambda r: r[1])
t0, t1 = rows[0][1], rows[-1][1]
def tenth(p): return min(9, int(p * 10))
for W in (3, 6, 12):
    cal = [[0, 0.0, 0.0, 0.0] for _ in range(10)]; ll = 0.0; llm = 0.0; n = 0; nh = 0
    buys = {g: {} for g in (0.0, 0.03, 0.05, 0.08)}
    t = t0 + 12 * 3600
    while t + 3600 <= t1:
        tr = [r for r in rows if t - W * 3600 <= r[1] < t]; te = [r for r in rows if t <= r[1] < t + 3600]
        t += 3600
        if len(tr) < 400 or len(te) < 200: continue
        w = fit(tr); nh += 1
        for tk, _, x, b, a, y in te:
            p = score(w, x, b, a); m = mid(b, a)
            c = cal[tenth(p)]; c[0] += 1; c[1] += p; c[2] += y; c[3] += m
            ll -= math.log(max(1e-6, p if y else 1 - p)); llm -= math.log(m if y else 1 - m); n += 1
            for gap in buys:
                for side, ask, chance, win in (("yes", a, p, y == 1.0), ("no", 1 - b, 1 - p, y == 0.0)):
                    if 0.02 <= ask <= 0.98 and chance - ask - fee(ask) >= gap and (tk, side) not in buys[gap]:
                        buys[gap][(tk, side)] = (ask, win)
    print(f"=== trailing {W} h, refit hourly, {nh} test hours, {n} readings; log loss score {ll/n:.4f} vs Kalshi mid {llm/n:.4f}")
    for k, c in enumerate(cal):
        if c[0]: print(f"  score {k/10:.1f}-{(k+1)/10:.1f}: n {c[0]:6d} mean score {c[1]/c[0]:.3f} | YES rate {c[2]/c[0]:.3f} | Kalshi mid {c[3]/c[0]:.3f}")
    for gap, d in buys.items():
        if d: print(f"  buy where chance - ask - fee >= {gap:.2f}: n {len(d)} win {sum(w for _, w in d.values())/len(d):.2f} net {sum(((1.0 if w else 0.0) - a - fee(a)) / a for a, w in d.values())/len(d):+.3f}")
        else: print(f"  buy gap {gap:.2f}: n 0")
