"""Is this a good time to buy? A score for any point in time on a market's curve, from the tape, refit as the market changes (walk-forward, every crypto 15-minute window since 2026-11-09).
Question (user, 2026-11-19: "a risk score based on any point in time on the curve ... a week's worth of tape ... it mutates and changes as the market as a whole changes"; "we aren't trying to beat the price, we are trying to look at the factors around it and decide if it's a good time to buy"): at a random moment of a market, from what was known then, do the price, the cushion, the time left and the look-backs say whether BUYING that side at its ask pays? Scored on what the buy returned, never on the YES result against the mid:
  held   the net per $1 staked of buying at the ask and holding to the result, fee in
  peak   the net per $1 of buying at the ask and selling at the best bid the curve gave afterwards (a round trip, fees on both legs; a ceiling, hindsight exit, a measure of the timing not an exit rule)
Moments: 24 drawn at random inside each market (90 s in to 90 s before the close): every point on the curve as likely as any other, no grid. A moment with no fresh quote (30 s) has no reading. Look-backs are SHARES OF THE TIME LEFT (1/30, 1/15, 2/15, 4/15 of it, the time left counted as no less than 90 s: riskscore's own 1/30, 1/15 and floor; the last two are my choice), never fixed seconds.
Readings, for the side being bought (signed so + is the side's way; units are the market's or the spot's own typical move; no coin is named): price (logit of the side's mid), cushion (ln(spot/target) over the spot's rms one-second move of the last 15 min x sqrt(seconds left)), time (share of the market gone x price), dm x4 (the side's mid change over each look-back over the mid's own rms one-second move so far x sqrt(look-back)), eff x2 (path efficiency of the mid over 2/15 and 4/15), sp x2 (the spot's travel over 1/15 and 4/15 in its own typical moves).
Fit: ridge regression of each target on the readings (+ a constant), both sides pooled, on the 7 days before each test day (refit daily: it can change); a test day needs 4 days behind it. Rule: a side is bought at its first moment on the test day whose predicted target is in the top fifth of the trailing week's own predictions (a quantile of its own history, not a fixed number); price 0.60-0.85, spread <= 0.03, one buy per market side. Reported pooled over test days against the first eligible moment of every side (no score), by day, with the weights first and last test day. Settings chosen after seeing nothing yet; the top fifth is a plain unit.
Stdlib only. Run: scripts/cloud.sh run scripts/risk_curve_walkforward.py"""
import bisect, collections, datetime, math, os, random, time
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(15)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
T0 = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
SHARES = (1 / 30, 1 / 15, 2 / 15, 4 / 15)
FLOOR = 90.0
MOMENTS = 24
NAMES = ["const", "price", "cushion", "time", "dm 1/30", "dm 1/15", "dm 2/15", "dm 4/15", "eff 2/15", "eff 4/15", "sp 1/15", "sp 4/15"]
def logit(p): p = min(0.98, max(0.02, p)); return math.log(p / (1 - p))
def clip(x, c): return max(-c, min(c, x))
def rms(seq):
    d = [b - a for a, b in zip(seq, seq[1:])]
    return math.sqrt(sum(x * x for x in d) / len(d)) if d else 0.0
rows = []  # (ticker, day, side, x[12], ask, spread, held, peak, ts)
t_start = time.time()
for ser in CRYPTO:
    ix = variants.crypto_index(ser)
    if not ix: continue
    W = st.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (ser, T0)).fetchall()
    if not W: continue
    G0 = int(T0 - 1200); G1 = int(float(W[-1]["close_ts"])) + 2
    its = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, G0 - 60, G1)).fetchall()
    spot = [None] * (G1 - G0 + 1); j = 0; last = None
    for k in range(G1 - G0 + 1):
        T = G0 + k
        while j < len(its) and float(its[j][0]) <= T: last = float(its[j][1]); j += 1
        spot[k] = last
    del its
    qs = collections.defaultdict(list)
    for q in tape.execute("SELECT ticker, ts, bid, ask FROM quotes WHERE ticker LIKE ? AND ts>? ORDER BY ticker, ts", (ser + "-%", T0)).fetchall():
        if q[2] is not None and q[3] is not None and float(q[3]) > 0: qs[q[0]].append((float(q[1]), float(q[2]), float(q[3])))
    for w in W:
        tk = w["ticker"]; Q = qs.get(tk)
        if not Q or len(Q) < 30: continue
        close = float(w["close_ts"]); opened = close - 900.0; strike = float(w["strike"]); yes_won = w["result"] == "yes"
        qts = [a[0] for a in Q]; n = len(Q)
        sufmax = [0.0] * (n + 1); sufmin = [1.0] * (n + 1)  # best YES bid and lowest YES ask from a quote on (the later curve)
        for i in range(n - 1, -1, -1): sufmax[i] = max(sufmax[i + 1], Q[i][1]); sufmin[i] = min(sufmin[i + 1], Q[i][2])
        mid1 = []; lastq = None; jq = 0
        for k in range(901):
            T = opened + k
            while jq < len(qts) and qts[jq] <= T: lastq = Q[jq]; jq += 1
            mid1.append((lastq[1] + lastq[2]) / 2 if lastq else None)
        rng = random.Random(tk)
        for k in sorted(rng.uniform(90.0, 810.0) for _ in range(MOMENTS)):
            T = opened + k; left = close - T
            jq = bisect.bisect_right(qts, T) - 1
            if jq < 0 or qts[jq] < T - 30: continue
            q = Q[jq]
            if q[2] - q[1] > 0.10: continue
            Lmax = int(SHARES[-1] * max(left, FLOOR)); ki = int(k)
            if ki - Lmax < 0 or any(v is None for v in mid1[ki - Lmax:ki + 1]): continue
            sg = max(rms([v for v in mid1[:ki + 1] if v is not None]), 0.0005)
            gi = int(T) - G0; sp = spot[gi - 900:gi + 1]
            if len(sp) < 901 or sp[0] is None or any(v is None for v in sp[-Lmax - 2:]): continue
            ls = [math.log(b2 / a2) for a2, b2 in zip(sp, sp[1:]) if a2 and b2]
            ssg = math.sqrt(sum(x * x for x in ls) / len(ls)) if ls else 0.0
            if ssg <= 0: continue
            mid = (q[1] + q[2]) / 2
            cush = clip(math.log(sp[-1] / strike) / (ssg * math.sqrt(max(left, FLOOR))), 8.0)
            dm = [clip((mid1[ki] - mid1[ki - max(1, int(sh * max(left, FLOOR)))]) / (sg * math.sqrt(sh * max(left, FLOOR))), 6.0) for sh in SHARES]
            eff = []
            for sh in (SHARES[2], SHARES[3]):
                L = max(2, int(sh * max(left, FLOOR))); sg2 = mid1[ki - L:ki + 1]; path = sum(abs(b2 - c2) for c2, b2 in zip(sg2, sg2[1:]))
                eff.append((sg2[-1] - sg2[0]) / path if path > 1e-9 else 0.0)
            spt = [clip(math.log(sp[-1] / sp[-1 - max(1, int(sh * max(left, FLOOR)))]) / (ssg * math.sqrt(sh * max(left, FLOOR))), 6.0) for sh in (SHARES[1], SHARES[3])]
            day = datetime.datetime.fromtimestamp(T, ET).strftime("%m-%d")
            for side in ("yes", "no"):
                sgn = 1.0 if side == "yes" else -1.0
                ask = q[2] if side == "yes" else 1 - q[1]
                pm = mid if side == "yes" else 1 - mid
                pr = logit(pm)
                x = [1.0, pr, sgn * cush, (k / 900.0) * pr] + [sgn * v for v in dm] + [sgn * v for v in eff] + [sgn * v for v in spt]
                win = yes_won if side == "yes" else not yes_won
                held = ((1.0 if win else 0.0) - ask - fee(ask)) / ask
                later = jq + 1  # the curve after this moment
                pk = sufmax[later] if side == "yes" else 1 - sufmin[later]
                peak = (pk - ask - fee(ask) - fee(pk)) / ask if later < n else -1.0
                rows.append((tk, day, side, x, ask, q[2] - q[1], held, peak, T))
    del qs, spot
    print(f"  {ser}: {len(W)} windows read ({time.time() - t_start:.0f}s, {len(rows)} side-moments so far)", flush=True)
days = sorted(set(r[1] for r in rows))
print(f"{len(rows)} side-moments over {len(set(r[0] for r in rows))} markets, days {days[0]} to {days[-1]}")
def solve(A, b):
    n = len(b); M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(M[k][i])); M[i], M[p] = M[p], M[i]
        d = M[i][i] or 1e-12
        for j in range(i, n + 1): M[i][j] /= d
        for k in range(n):
            if k != i:
                f = M[k][i]
                if f:
                    for j in range(i, n + 1): M[k][j] -= f * M[i][j]
    return [M[i][n] for i in range(n)]
def ridge(data, target):
    m = len(NAMES); A = [[0.0] * m for _ in range(m)]; b = [0.0] * m
    for r in data:
        x = r[3]; y = r[target]
        for i in range(m):
            b[i] += x[i] * y; xi = x[i]; Ai = A[i]
            for j in range(i + 1): Ai[j] += xi * x[j]
    for i in range(m):
        A[i][i] += 1e-3 * (len(data) / 1000.0 + 1)
        for j in range(i): A[j][i] = A[i][j]
    return solve(A, b)
def pred(w, x): return sum(a * b for a, b in zip(w, x))
TARGETS = {"held": 6, "peak": 7}
acc = {t: {"n": 0, "held": 0.0, "peak": 0.0, "wins": 0} for t in TARGETS}; base = {"n": 0, "held": 0.0, "peak": 0.0}; allm = {"n": 0, "held": 0.0, "peak": 0.0}
weights = {}; perday = []
for ti, day in enumerate(days):
    if ti < 4: continue
    tr = [r for r in rows if r[1] in days[max(0, ti - 7):ti] and 0.60 <= r[4] <= 0.85 and r[5] <= 0.03]
    te = sorted([r for r in rows if r[1] == day and 0.60 <= r[4] <= 0.85 and r[5] <= 0.03], key=lambda r: r[8])
    if len(tr) < 2000 or len(te) < 100: continue
    seen = set()
    for r in te:
        allm["n"] += 1; allm["held"] += r[6]; allm["peak"] += r[7]
        if (r[0], r[2]) not in seen: seen.add((r[0], r[2])); base["n"] += 1; base["held"] += r[6]; base["peak"] += r[7]
    line = [f"{day} eligible {len(te):5d}"]
    for name, col in TARGETS.items():
        w = ridge(tr, col); weights[(name, day)] = w
        ps = sorted(pred(w, r[3]) for r in tr); cut = ps[int(0.8 * len(ps))]
        seen = set(); n = 0; tot_h = 0.0; tot_p = 0.0
        for r in te:
            if (r[0], r[2]) in seen or pred(w, r[3]) < cut: continue
            seen.add((r[0], r[2])); n += 1; tot_h += r[6]; tot_p += r[7]; a = acc[name]; a["n"] += 1; a["held"] += r[6]; a["peak"] += r[7]; a["wins"] += r[6] > -0.3
        line.append(f"by {name}: n {n} held {tot_h / n:+.3f} peak {tot_p / n:+.3f}" if n else f"by {name}: n 0")
    perday.append("  ".join(line)); print(perday[-1], flush=True)
print(f"pooled over test days (net per $1 staked, fee in):")
print(f"  every eligible moment (no score)          n {allm['n']:6d} held {allm['held']/max(1,allm['n']):+.3f} peak {allm['peak']/max(1,allm['n']):+.3f}")
print(f"  first eligible moment of each side         n {base['n']:6d} held {base['held']/max(1,base['n']):+.3f} peak {base['peak']/max(1,base['n']):+.3f}")
for name in TARGETS:
    a = acc[name]; print(f"  top fifth by predicted {name:4s} (first such moment per side) n {a['n']:5d} held {a['held']/max(1,a['n']):+.3f} peak {a['peak']/max(1,a['n']):+.3f}")
for name in TARGETS:
    ds = sorted(d for (n, d) in weights if n == name)
    if ds:
        print(f"weights for the predicted {name} (first test day {ds[0]} -> last {ds[-1]}):")
        for i, nm in enumerate(NAMES): print(f"    {nm:9s} {weights[(name, ds[0])][i]:+.3f} -> {weights[(name, ds[-1])][i]:+.3f}")
