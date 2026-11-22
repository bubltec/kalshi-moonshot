"""PriskP bet-by-bet review (2026-11-22, user via parent): the crypto bets bought after a FALL over 30 minutes that is BENDING UP (own-history terciles: 30-minute slope tercile 1 and curvature tercile 3, side view), from minute 10, held to settlement.
For each of the biggest winners and losers that have raw data: every reading is RECOMPUTED here by hand from the raw settlement-index ticks (independent code: no kalshi_moonshot import, the shape fitted by a plain 3x3 least-squares solve, not the model's shortcut) and compared with what the study recorded; then how the bet went (spot against the strike by minute, the side's price path). No old shape table, class or saved model is read.
Run: python3 scripts/priskp_segment_review.py crypto_bets_m10.json.gz round_trip_raw.json.gz [N]"""
import bisect, gzip, json, math, sys
rows = json.load(gzip.open(sys.argv[1])); raw = json.load(gzip.open(sys.argv[2])); N = int(sys.argv[3]) if len(sys.argv) > 3 else 8
seg = [r for r in rows if r["states"].get("t:sl30") == 1 and r["states"].get("t:cv30") == 3]
have = [r for r in seg if r["ticker"] in raw]
print(f"{len(seg)} bets in the segment (30-min slope tercile 1, curvature tercile 3); {len(have)} have raw ticks; segment ahead {100 * sum(1 for r in seg if r['net'] > 0) / len(seg):.0f}%, paid {sum(r['price'] for r in seg) / len(seg):.3f}\n")


def solve3(A, b):
    M = [A[i][:] + [b[i]] for i in range(3)]
    for i in range(3):
        p = max(range(i, 3), key=lambda k: abs(M[k][i])); M[i], M[p] = M[p], M[i]
        for k in range(3):
            if k != i:
                f = M[k][i] / M[i][i]
                M[k] = [a - f * c for a, c in zip(M[k], M[i])]
    return [M[i][3] / M[i][i] for i in range(3)]


def by_hand(T, V, t, strike, left):
    j = bisect.bisect_right(T, t) - 1
    def u(sec):
        i = bisect.bisect_left(T, t - sec)
        sq = sum(math.log(V[k] / V[k - 1]) ** 2 for k in range(i + 1, j + 1))
        return math.sqrt(sq / (T[j] - T[i]) * 60.0)
    u15, uh = u(900.0), u(3600.0)
    lm = left / 60.0
    def avg(sec):
        a = V[bisect.bisect_left(T, t - sec): j + 1]; return sum(a) / len(a)
    sp = {m: avg(60.0 * m) for m in (1, 3, 5)}
    cl = lambda x, a: max(-a, min(a, x))
    cz = {m: cl(math.log(sp[m] / strike) / (u15 * math.sqrt(lm)), 8.0) for m in sp}
    blend = sum(sp.values()) / 3.0
    out = {"czb": sum(cz.values()) / 3.0, "tr13": cl(math.log(sp[1] / sp[3]) / u15, 30.0), "vr": u15 / uh - 1.0}
    i30 = bisect.bisect_left(T, t - 1800.0); w = V[i30: j + 1]
    out["pos30"] = (blend - min(w)) / (max(w) - min(w)); out["mv30"] = cl(math.log(blend / V[i30]) / u15, 60.0)
    for mins in (15, 30, 60):
        lo = t - 60.0 * mins; n = 12
        e = [bisect.bisect_left(T, lo + 60.0 * mins * k / n) for k in range(n)] + [bisect.bisect_right(T, t)]
        ys = [sum(V[e[k]: max(e[k + 1], e[k] + 1)]) / len(V[e[k]: max(e[k + 1], e[k] + 1)]) for k in range(n)]
        y = [math.log(v / ys[-1]) / u15 for v in ys]; x = [-1.0 + 2.0 * (k + 0.5) / n for k in range(n)]
        S = lambda f: sum(f(a) for a in x)
        A = [[n, S(lambda a: a), S(lambda a: a * a)], [S(lambda a: a), S(lambda a: a * a), S(lambda a: a ** 3)], [S(lambda a: a * a), S(lambda a: a ** 3), S(lambda a: a ** 4)]]
        a0, b1, c2 = solve3(A, [sum(y), sum(p * q for p, q in zip(x, y)), sum(p * p * q for p, q in zip(x, y))])
        sx2 = sum(a * a for a in x); b = sum(p * q for p, q in zip(x, y)) / sx2; my = sum(y) / n
        ssy = sum((c - my) ** 2 for c in y); ft = 1.0 - sum((c - my - b * a) ** 2 for a, c in zip(x, y)) / ssy if ssy > 1e-18 else 1.0
        out[f"sl{mins}"], out[f"cv{mins}"], out[f"ft{mins}"] = max(-60, min(60, b1)), max(-60, min(60, c2)), max(0.0, min(1.0, ft))
    sh = sum(1 for k in range(bisect.bisect_left(T, t - 1800.0), j + 1) if V[k] > strike) / (j + 1 - bisect.bisect_left(T, t - 1800.0))
    out["history_share_above"] = sh
    return out


def review(r):
    d = raw[r["ticker"]]; T = [x[0] for x in d["ticks"]]; V = [x[1] for x in d["ticks"]]; strike, close = d["strike"], d["close"]
    t = close - 900.0 + r["kk"]; left = close - t; f = r["f"]; sg = 1 if r["side"] == "yes" else -1
    h = by_hand(T, V, t, strike, left)
    bad = []
    for name in ("czb", "tr13", "vr", "pos30", "mv30", "sl15", "sl30", "sl60", "cv15", "cv30", "cv60", "ft15", "ft30", "ft60"):
        rec = f[name]
        if abs(h[name] - rec) > 0.02 + 0.02 * abs(rec): bad.append(f"{name} hand {h[name]:+.3f} vs study {rec:+.3f}")
    lastv = V[bisect.bisect_right(T, close) - 1]
    path = []
    for m in (10, 11, 12, 13, 14, 15):
        k = bisect.bisect_right(T, close - 900.0 + 60.0 * m) - 1
        path.append(f"{m}:{100 * math.log(V[k] / strike) / 1.0:+.2f}%")
    print(f"{r['ticker']} {r['side']:3s} bought min {r['minute']:.1f} at {r['price']:.2f}  ${r['net']:+.2f}  {'WON' if r['net'] > 0 else 'LOST'}  left {left:.0f}s")
    print(f"   side view: slope30 {sg * h['sl30']:+.2f} curv30 {sg * h['cv30']:+.2f} slope15 {sg * h['sl15']:+.2f} curv15 {sg * h['cv15']:+.2f} slope60 {sg * h['sl60']:+.2f} | cushion {sg * h['czb']:+.2f} trend {sg * h['tr13']:+.2f} vol {h['vr']:+.2f} | share of last 30 min on the winning side {(h['history_share_above'] if sg > 0 else 1 - h['history_share_above']):.2f} | price move at the buy {r['raw']['price']:+.2f}, dip {r['raw']['pricedip']:+.2f}")
    print(f"   hand check vs study: {'ALL 14 readings agree' if not bad else 'DISAGREE: ' + '; '.join(bad)}")
    print(f"   spot vs strike by minute (%): {' '.join(path)}; settled spot {100 * math.log(lastv / strike):+.3f}% -> {'yes' if lastv > strike else 'no'}\n")
    return not bad, h


ok = 0; n = 0
for title, grp in (("BIGGEST WINNERS", sorted(have, key=lambda r: -r["net"])[:N]), ("BIGGEST LOSERS", sorted(have, key=lambda r: r["net"])[:N])):
    print("=== " + title)
    for r in grp:
        g, _ = review(r); ok += g; n += 1
print(f"hand check: {ok} of {n} reviewed bets had all 14 readings agree")
