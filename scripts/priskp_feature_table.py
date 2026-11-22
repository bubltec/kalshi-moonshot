"""PriskP per-feature table (2026-11-22, user via parent: "yes continue with the single per-feature table").
QUESTION: for each price-free reading of the crypto run (11 crypto series on their Kalshi settlement indexes, bought from minute 10, one bet a market as the lane places its order, held to settlement, fee on both legs), cut into its own-history fifths (the series' previous 24 h in the same 3 minutes of the market, both sides), what is the net per $1, the share ahead, the average price paid, the break-even (price plus fee) and whether the share ahead beat it?
Input: crypto_bets_m10.json.gz from scripts/binary_risk_round_trips.py UNIVERSE=crypto EXIT=hold FEATURES=1 SHAPES=1 ONLY=base (path in argv[1]).
Verdicts, rules chosen AFTER seeing the results (stated as such): over the fifths with 30 or more bets, spread = net of the highest-reading fifth minus the lowest; helps = spread >= +2c and net rises across the fifths (rank correlation >= 0.5); backwards = spread <= -2c and rank correlation <= -0.7 (adjust: the side's reading should be read the other way or capped); U = the best or worst fifth is inside and differs from both ends by 2c or more (adjust the cutoff); remove = |spread| < 2c and no U. A fifth under 30 bets is marked too few. Anomaly and unread are shown, not scored. Terciles (t: rows) use 1-3.
"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)  # the same fee as strategy.fee_per_contract (not imported: that pulls httpx, which a local run lacks)
rows = json.load(gzip.open(sys.argv[1]))
names = list(rows[0]["states"])
print(f"{len(rows)} bets, crypto, from minute 10, held to settlement, fee on both legs\n")


def cell(g):
    n = len(g)
    if n < 30: return None
    win = sum(1 for r in g if r["net"] > 0) / n
    be = sum(r["price"] + fee(r["price"]) for r in g) / n
    return n, 100.0 * sum(r["net"] for r in g) / (10.0 * n), win, sum(r["price"] for r in g) / n, be


def rank_corr(xs):
    n = len(xs)
    if n < 3: return 0.0
    rx = list(range(n)); ordr = sorted(range(n), key=lambda i: xs[i]); ry = [0] * n
    for k, i in enumerate(ordr): ry[i] = k
    mx = (n - 1) / 2.0
    num = sum((a - mx) * (b - mx) for a, b in zip(rx, ry)); den = sum((a - mx) ** 2 for a in rx)
    return num / den if den else 0.0


summary = []
for name in names:
    scale = (1, 2, 3) if name.startswith("t:") else (1, 2, 3, 4, 5)
    print(f"--- {name}")
    cs = []
    for st in scale:
        g = [r for r in rows if r["states"][name] == st]
        c = cell(g)
        cs.append(c)
        print(f"   {st}: " + (f"n {len(g):4d} too few" if c is None else "n %4d  %+6.1fc/$1  ahead %2.0f%%  paid %.3f  need %2.0f%%  %s" % (c[0], c[1], 100 * c[2], c[3], 100 * c[4], "BEAT" if c[2] > c[4] else "no")))
    for st in ("anomaly", "unread"):
        g = [r for r in rows if r["states"][name] == st]
        c = cell(g)
        if g: print(f"   {st}: " + (f"n {len(g):4d} too few" if c is None else "n %4d  %+6.1fc/$1  ahead %2.0f%%" % (c[0], c[1], 100 * c[2])))
    ok = [(i, c) for i, c in enumerate(cs) if c]
    if len(ok) < 3: verdict = "too few to judge"
    else:
        v = [c[1] for _, c in ok]; spread = v[-1] - v[0]; rc = rank_corr(v)
        inner_hi = max(v[1:-1]) - max(v[0], v[-1]); inner_lo = min(v[1:-1]) - min(v[0], v[-1])
        if spread >= 2 and rc >= 0.5: verdict = f"HELPS (net rises {v[0]:+.1f}c to {v[-1]:+.1f}c)"
        elif spread <= -2 and rc <= -0.5: verdict = f"ADJUST, backwards (net falls {v[0]:+.1f}c to {v[-1]:+.1f}c: the high reading is the loss)"
        elif inner_hi >= 2 or inner_lo <= -2: verdict = f"ADJUST, U-shaped (ends {v[0]:+.1f}c and {v[-1]:+.1f}c, middle best {max(v):+.1f}c worst {min(v):+.1f}c)"
        else: verdict = f"REMOVE (no gradient: {v[0]:+.1f}c to {v[-1]:+.1f}c)"
    beat = sum(1 for c in cs if c and c[2] > c[4])
    print(f"   => {verdict}; fifths that beat price plus fee: {beat}\n")
    summary.append((name, verdict, beat))
print("SUMMARY")
for n, v, b in summary: print(f"  {n:9s} {v}  [beat {b}]")
