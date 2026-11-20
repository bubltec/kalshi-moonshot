"""Does a 1-100 score of "take this bet or not", built from the curve's history, the cushion, the time left and the value (the price), order the bets by what they paid, and does the price alone do it? (walk-forward, crypto 15-minute markets, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "the history of curve and cushion, time left and value give me a score about whether to take the bet or not. The price alone does not"): for each side (YES or NO) in the 0.60-0.85 band (spread <= 0.03), the readings are turned to that side's view (the YES readings that carry a direction change sign for NO; the previous-markets flip count does not), and a logistic on "the side paid" is fit on the earlier days, every in-band reading. The score is the percentile (1-100), among the earlier days' sides, of the model's expected net per $1 (chance - ask - fee) / ask. Sets: P price alone (the value only), C value + cushion + time + every look-back, D all fifteen. On the held-out day, each side's first in-band reading per market is bucketed by score in fifths: n, win rate, mean ask, net per $1 held to the result, fee in. A score that helps shows net rising with it, and beating P's buckets. Fifths and ridge 1.0 are set before the run. No coin is named.
Folds: train 11-17 -> test 11-18; train 11-17+11-18 -> test 11-19 (ET days). Every other reading (60 s apart) to keep it light; stdlib only.
Run: scripts/cloud.sh run scripts/risk_score_take_bet.py"""
import bisect, datetime, json, math, os
from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
N = list(riskscore.NAMES)
UNSIGNED = {N.index("flips")}  # the previous markets' flip count has no direction
SETS = {"P price alone": ["price"], "C value+cushion+time+look-backs": ["price", "cushion", "time", "t15", "t30", "t60", "nudge", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far"], "D all fifteen": N}
sides = []  # (ticker, day, side, oriented x, ask, win)
for r in st.execute("SELECT ticker, ts, x, yes_bid, yes_ask FROM risk_readings ORDER BY ts").fetchall():
    tk = r["ticker"]
    if not tk.startswith(CRYPTO) or res.get(tk) not in ("yes", "no") or int(r["ts"]) % 60 >= 30: continue
    x = json.loads(r["x"]) if isinstance(r["x"], str) else list(r["x"])
    if len(x) != len(N) or r["yes_bid"] is None or r["yes_ask"] is None: continue
    b, a = float(r["yes_bid"]), float(r["yes_ask"])
    if a - b > 0.03: continue
    day = datetime.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d")
    for side, ask in (("yes", a), ("no", 1 - b)):
        if not 0.60 <= ask <= 0.85: continue
        sg = 1.0 if side == "yes" else -1.0
        sides.append((tk, day, side, [v if i in UNSIGNED else sg * v for i, v in enumerate(x)], ask, 1.0 if res[tk] == side else 0.0))
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
        for _, _, _, x, _, y in data:
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
for train_days, test_day in ((["11-17"], "11-18"), (["11-17", "11-18"], "11-19")):
    tr = [r for r in sides if r[1] in train_days]; te = [r for r in sides if r[1] == test_day]
    seen = set(); first = []
    for r in te:
        if (r[0], r[2]) not in seen: seen.add((r[0], r[2])); first.append(r)
    print(f"=== train {'+'.join(train_days)} ({len(tr)}) -> test {test_day} ({len(first)} sides)")
    for name, names in SETS.items():
        idx = [N.index(n) for n in names]; w = fit(tr, idx)
        ref = sorted(value(w, idx, x, a) for _, _, _, x, a, _ in tr)
        out = []
        for lo in (1, 21, 41, 61, 81):
            g = []
            for _, _, _, x, a, win in first:
                sc = max(1, min(100, 1 + int(100 * bisect.bisect_left(ref, value(w, idx, x, a)) / len(ref))))
                if lo <= sc < lo + 20 or (lo == 81 and sc == 100): g.append((a, win))
            if not g: out.append(f"{lo}-{lo+19}: n 0"); continue
            net = sum(((1.0 if win else 0.0) - a - fee(a)) / a for a, win in g) / len(g)
            out.append(f"{lo}-{lo+19}: n {len(g)} win {sum(w_ for _, w_ in g)/len(g):.2f} ask {sum(a for a, _ in g)/len(g):.2f} net {net:+.3f}")
        print(f"  {name:34s} " + " | ".join(out))
