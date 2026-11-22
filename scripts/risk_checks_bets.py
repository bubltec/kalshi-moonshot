"""THE CHECKS MODEL'S OWN RUN, bet by bet (2026-11-21, user: "we are supposed to be testing that and the runs we did with it. Comparing what losses and won"). Reads the bets scripts/binary_risk_round_trips.py wrote (round_trip_bets.json: the formula's bets, all five directional checks moving the resting price, closed by commodity_exit) and the raw perp ticks and Kalshi quotes it kept for each series' biggest losers and winners (round_trip_raw.json.gz).
1. Per-bet rows: the biggest losers and winners with each check's reading and state (fifth 1-2 = NO, 3 = WEAK, 4-5 = YES of the market's own history), how far the price was moved, how it ended.
2. HAND CHECK: each check's reading recomputed here from the raw ticks and quotes, in code written apart from the model's (no call into binaryrisk), beside the reading the run used.
3. Winners against losers per check, on the formula's bets: by state, n, ahead, dollars; and how many checks said NO.
    python3 scripts/risk_checks_bets.py [UNIVERSE=m10|late]"""
import bisect, glob, gzip, json, math, sys

U = dict(a.split("=") for a in sys.argv[1:]).get("UNIVERSE", "m10")
J = sorted(glob.glob("job-results/*/files/round_trip_raw.json.gz"))[-1]
RAW = json.load(gzip.open(J))
ALL = json.load(open(J.replace("round_trip_raw.json.gz", "round_trip_bets.json")))
B = sorted([r for r in ALL if r["universe"] == U and r["rule"] == "formula"], key=lambda r: r["net"])
CHECKS = ("cushion", "trend", "history", "volatility", "price", "pricedip")
DIRECT = ("cushion", "trend", "history", "price", "pricedip")
st = lambda v: {1: "NO", 2: "NO", 3: "weak", 4: "YES", 5: "YES"}.get(v, str(v)[:4])
print(f"the formula's bets, buying from {'minute 10' if U == 'm10' else 'the last 3 minutes'}: {len(B)} bets, {sum(r['net'] for r in B):+.2f}$ on $10 stakes, ahead {sum(1 for r in B if r['net'] > 0)}, behind {sum(1 for r in B if r['net'] <= 0)}; average win {sum(r['net'] for r in B if r['net'] > 0) / max(1, sum(1 for r in B if r['net'] > 0)):+.2f}, average loss {sum(r['net'] for r in B if r['net'] <= 0) / max(1, sum(1 for r in B if r['net'] <= 0)):+.2f}")


def show(title, g):
    print("\n" + title + "\n  market                         side paid  min moved ended           net$  | " + " | ".join("%-13s" % c for c in CHECKS))
    for r in g:
        end = "settled " + ("won" if r["won"] else "LOST") if r["sold"] is None else "%-10s %.2f" % (r["step"], r["sold"])
        print("  %-30s %-3s  %.2f %4.1f %4.1f  %-15s %+6.2f | " % (r["ticker"], r["side"], r["price"], r["minute"], r["d"], end, r["net"]) + " | ".join("%+6.2f %-6s" % (r["raw"][c], st(r["states"][c])) if r["raw"][c] is not None else "   n/a %-6s" % st(r["states"][c]) for c in CHECKS))


show("THE 15 BIGGEST LOSERS (reading, then its state; 'moved' = typical minutes the resting buy sat under the ask)", B[:15])
show("THE 15 BIGGEST WINNERS", B[::-1][:15])


def hand(r):
    """Each check's reading for the bet's side, from the raw ticks and quotes alone."""
    w = RAW[r["ticker"]]
    T, V = [x[0] for x in w["ticks"]], [x[1] for x in w["ticks"]]
    close, K = w["close"], w["strike"]
    t = close - 900.0 + r["kk"]
    j = bisect.bisect_right(T, t) - 1
    sg = 1.0 if r["side"] == "yes" else -1.0
    def rate(sec):  # typical minute: root of (summed squared log tick changes / seconds spanned x 60)
        i = bisect.bisect_left(T, t - sec)
        return math.sqrt(sum(math.log(V[k] / V[k - 1]) ** 2 for k in range(i + 1, j + 1)) / (T[j] - T[i]) * 60.0)
    mean = lambda sec: (lambda a: sum(a) / len(a))(V[bisect.bisect_left(T, t - sec):j + 1])
    u15, uh, left_min = rate(900.0), rate(3600.0), (close - t) / 60.0
    a1, a3, a5 = mean(60.0), mean(180.0), mean(300.0)
    cushion = sg * sum(math.log(a / K) / (u15 * math.sqrt(left_min)) for a in (a1, a3, a5)) / 3.0
    trend = sg * math.log(a1 / a3) / u15
    i30 = bisect.bisect_left(T, t - 1800.0)
    over = sum(1 for k in range(i30, j + 1) if V[k] > K) / (j + 1 - i30)
    history = over if r["side"] == "yes" else 1.0 - over
    Q = [q for q in w["quotes"] if q[1] > 0 and q[2] > 0]
    qt = [q[0] for q in Q]
    def mid(x):
        k = bisect.bisect_right(qt, x) - 1
        m = (Q[k][1] + Q[k][2]) / 2.0
        return m if r["side"] == "yes" else 1.0 - m
    first = math.ceil(qt[0] - (close - 960.0))  # the first whole second with a quote, on the run's grid (second 0 = a minute before the open)
    L = min(300, r["kk"] + 60 - first)
    price = dip = None
    if L >= 60:
        g = [mid(t - 5.0 * x) for x in range(L // 5, -1, -1)]
        pu = math.sqrt(sum((b - a) ** 2 for a, b in zip(g, g[1:])) / (len(g) - 1) * 12.0)
        if pu > 0:
            back = min(180, L)
            price, dip = (g[-1] - mid(t - back)) / (pu * math.sqrt(back / 60.0)), -(max(g) - g[-1]) / pu
    return {"cushion": cushion, "trend": trend, "history": history, "volatility": u15 / uh - 1.0, "price": price, "pricedip": dip}


H = [(r, hand(r)) for r in B if r["ticker"] in RAW]
print(f"\nHAND CHECK on {len(H)} bets with raw ticks and quotes (each series' biggest losers and winners): the run's reading / recomputed here")
for r, h in sorted(H, key=lambda x: x[0]["net"])[:6] + sorted(H, key=lambda x: -x[0]["net"])[:6]:
    print("  %-30s %-3s net %+6.2f | " % (r["ticker"], r["side"], r["net"]) + " | ".join("%s %s/%s" % (c[:5], "%+.2f" % r["raw"][c] if r["raw"][c] is not None else "n/a", "%+.2f" % h[c] if h[c] is not None else "n/a") for c in CHECKS))
for c in CHECKS:
    pr = [(r["raw"][c], h[c]) for r, h in H if r["raw"][c] is not None and h[c] is not None]
    d = sorted(abs(a - b) for a, b in pr)
    miss = sum(1 for r, h in H if (r["raw"][c] is None) != (h[c] is None))
    print("  %-10s n %3d  within 0.02: %3d (%3.0f%%)  median gap %.4f  worst %.3f%s" % (c, len(d), sum(1 for e in d if e <= 0.02), 100.0 * sum(1 for e in d if e <= 0.02) / max(1, len(d)), d[len(d) // 2] if d else 0, d[-1] if d else 0, f"  (read by one and not the other on {miss})" if miss else ""))

row = lambda g: "n %4d  ahead %3.0f%%  %+8.2f$  (%+5.1fc per $1)" % (len(g), 100.0 * sum(1 for r in g if r["net"] > 0) / len(g), sum(r["net"] for r in g), 10.0 * sum(r["net"] for r in g) / len(g)) if g else "n    0"
print("\nWINNERS AGAINST LOSERS PER CHECK, the formula's bets (a NO or weak check lowered the price; the bet still filled)")
print("  all                : " + row(B))
for c in CHECKS:
    print("  " + c)
    for name, keys in (("NO   (fifths 1-2)", (1, 2)), ("weak (fifth 3)", (3,)), ("YES  (fifths 4-5)", (4, 5)), ("unread", ("unread",))):
        g = [r for r in B if r["states"][c] in keys]
        if g: print("    %-18s %s" % (name, row(g)))
print("  how many of the five directional checks said NO")
for k in range(6):
    g = [r for r in B if sum(1 for c in DIRECT if r["states"][c] in (1, 2)) == k]
    if g: print("    %d                  %s" % (k, row(g)))
print("  how far the price was moved (typical minutes under the ask)")
for lo, hi in ((0, 0.01), (0.01, 1.01), (1.01, 2.01), (2.01, 9)):
    g = [r for r in B if lo <= r["d"] < hi]
    if g: print("    %.1f to %.1f         %s" % (lo, min(hi, 5), row(g)))
print("  how it ended")
for k in sorted({r["step"] for r in B}):
    g = [r for r in B if r["step"] == k]
    print("    %-18s %s  average %+.2f" % (k, row(g), sum(r["net"] for r in g) / len(g)))
