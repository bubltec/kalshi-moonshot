"""Does an ADJUSTMENT from the look-back, the cushion and the time remaining, added to Kalshi's price, beat the price? (walk-forward, crypto 15-minute markets, risk_readings since 2026-11-17)
Question (user, 2026-11-19: "the score is a number added or subtracted to Kalshi's price, not a replacement for it"): the first fit (risk_score_lookback_fit.py) let the price be a free weight, so it learned a replacement. Here the price is an OFFSET: chance = sigmoid(logit(mid) + w . readings), no price weight, no intercept, so every reading with weight zero returns Kalshi's mid. The weights are fit on the YES result, ridge 1.0, from three sets of the recorded readings, on the earlier days only, and score the later day: (B) the readings the live score adds (less the price), (C) cushion + time + every look-back (t15 t30 t60 nudge trend3 trend15 trend5 near/mid/far), (D) all fourteen. Also reported: the mean size of the adjustment in points of chance. The ridge is a setting chosen before the run. For each, the held-out log loss against the mid, and a buy rule scored on the held-out day: a side is bought at its first reading where the chance minus ask minus fee is at least a gap (0, 0.03, 0.05, 0.08), price 0.60-0.85, spread <= 0.03, one buy per market side; net per $1 held to the result, fee in. No coin is named. Gaps are plain units set before the run.
Folds: train 11-17 -> test 11-18; train 11-17+11-18 -> test 11-19 (ET days). Every other reading is used (60 s apart) to keep the fit light; stdlib only (the image has no numpy).
Run: scripts/cloud.sh run scripts/risk_score_adjust_fit.py"""
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
for train_days, test_day in ((["11-17"], "11-18"), (["11-17", "11-18"], "11-19")):
    tr = [r for r in rows if r[1] in train_days]; te = [r for r in rows if r[1] == test_day]
    if len(tr) < 3000 or len(te) < 500: print(f"fold {test_day}: too few readings ({len(tr)}, {len(te)})"); continue
    ll_mid = -sum(math.log(mid(b, a) if y else 1 - mid(b, a)) for _, _, _, b, a, y in te) / len(te)
    print(f"=== train {'+'.join(train_days)} ({len(tr)}) -> test {test_day} ({len(te)}); log loss of Kalshi's mid {ll_mid:.4f}")
    for name, names in SETS.items():
        idx = [N.index(n) for n in names]; w = fit(tr, idx)
        ll = -sum(math.log(max(1e-6, chance(w, idx, x, b, a) if y else 1 - chance(w, idx, x, b, a))) for _, _, x, b, a, y in te) / len(te)
        adj = sum(abs(chance(w, idx, x, b, a) - mid(b, a)) for _, _, x, b, a, _ in te) / len(te)
        out = [f"{name:34s} loss {ll:.4f} ({ll - ll_mid:+.4f} vs mid) mean |adj| {adj*100:.1f} pts"]
        for thr in (0.0, 0.03, 0.05, 0.08):
            seen = set(); n = 0; wins = 0; tot = 0.0
            for tk, _, x, b, a, y in te:
                p = chance(w, idx, x, b, a)
                for side, ask, pp, win in (("yes", a, p, y == 1.0), ("no", 1 - b, 1 - p, y == 0.0)):
                    if (tk, side) in seen or not 0.60 <= ask <= 0.85 or a - b > 0.03: continue
                    if pp - ask - fee(ask) >= thr:
                        seen.add((tk, side)); n += 1; wins += win; tot += ((1.0 if win else 0.0) - ask - fee(ask)) / ask
            out.append(f"gap>={thr:.2f}: n {n} net {tot / n:+.3f}" if n else f"gap>={thr:.2f}: n 0")
        print("  " + " | ".join(out))
