"""PriskP: how the market behaved over the past hour before the winning and the losing bets (2026-11-22, user: "we need to look at how the market has behaved over the past hour from these winning bets as well. That's where we will find the pattern to stick to").
Input: crypto_bets_m10.json.gz (scripts/binary_risk_round_trips.py UNIVERSE=crypto EXIT=hold SHAPES=1 FEATURES=1 ONLY=base), each bet with its 'hr' hour profile in the side's view and the market's own typical minute: 12 five-minute levels against the strike (oldest first), share of each 10-minute block on the side's winning side (6), strike crossings, minutes since the hour's high and low, range, drift, path efficiency.
Parts: (1) the average hour path of winners and losers, all bets and inside the "falling over 30 minutes, bending up" segment; (2) each hour feature cut into fifths of the pooled sample (DISCOVERY cuts, not the prior-24-hour cuts of the checks: said so), net per $1, share ahead, price paid, break-even, BEAT, and the same fifth in the first and the second half of the days (a pattern counts only if it is positive in both halves); (3) the segment split by the same hour features. No old table is read.
Run: python3 scripts/priskp_hour_pattern.py crypto_bets_m10.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = [r for r in json.load(gzip.open(sys.argv[1])) if r.get("hr")]
rows.sort(key=lambda r: r["close"]); half = rows[len(rows) // 2]["close"]
seg = lambda r: r["states"].get("t:sl30") == 1 and r["states"].get("t:cv30") == 3
print(f"{len(rows)} bets with an hour profile; halves split at close {half:.0f} ({sum(1 for r in rows if r['close'] < half)} / {sum(1 for r in rows if r['close'] >= half)})\n")


def cell(g):
    n = len(g)
    if n < 30: return None
    win = sum(1 for r in g if r["net"] > 0) / n
    return n, 100.0 * sum(r["net"] for r in g) / (10.0 * n), win, sum(r["price"] for r in g) / n, sum(r["price"] + fee(r["price"]) for r in g) / n


def fmt(c):
    return "too few" if c is None else "n %4d %+6.1fc ahead %2.0f%% paid %.3f need %2.0f%% %s" % (c[0], c[1], 100 * c[2], c[3], 100 * c[4], "BEAT" if c[2] > c[4] else "no")


print("(1) AVERAGE HOUR PATH, side view, in the market's own typical minutes above(+)/below(-) the strike; five-minute levels from 60 min ago to now")
for title, pool in (("all bets", rows), ("falling-30-min-bending-up segment", [r for r in rows if seg(r)])):
    for lab, grp in (("winners", [r for r in pool if r["net"] > 0]), ("losers", [r for r in pool if r["net"] <= 0])):
        if not grp: continue
        m = [sum(r["hr"]["bins"][b] for r in grp) / len(grp) for b in range(12)]
        bl = [sum(r["hr"]["blocks"][b] for r in grp) / len(grp) for b in range(6)]
        print(f"  {title:34s} {lab:8s} n {len(grp):4d} levels " + " ".join("%+5.1f" % x for x in m) + " | share on winning side by 10-min block " + " ".join("%.2f" % x for x in bl))
print()
FEATS = [("bin%02d" % b, lambda r, b=b: r["hr"]["bins"][b]) for b in range(12)] + [("block%d" % b, lambda r, b=b: r["hr"]["blocks"][b]) for b in range(6)] + \
        [(k, lambda r, k=k: r["hr"][k]) for k in ("cross", "hi_ago", "lo_ago", "range", "drift", "eff", "u15")]
print("(2) EACH HOUR FEATURE BY FIFTHS OF THE POOLED SAMPLE (discovery cuts), whole sample, then the first half | the second half of the days")
good = []
for name, fn in FEATS:
    xs = sorted(fn(r) for r in rows)
    cuts = [xs[int(len(xs) * q)] for q in (0.2, 0.4, 0.6, 0.8)]
    fifth = lambda r: 1 + sum(1 for c in cuts if fn(r) > c)
    print(f"--- {name}  (cuts {' '.join('%.3g' % c for c in cuts)})")
    for f5 in range(1, 6):
        g = [r for r in rows if fifth(r) == f5]
        a, b = cell([r for r in g if r["close"] < half]), cell([r for r in g if r["close"] >= half])
        print(f"   {f5}: {fmt(cell(g))}   | halves: {'%+.1fc' % a[1] if a else 'few'} {'%+.1fc' % b[1] if b else 'few'}")
        if a and b and a[1] > 0 and b[1] > 0: good.append((name, f5, cell(g)))
print("\nFIFTHS POSITIVE IN BOTH HALVES: " + ("; ".join(f"{n} fifth {f}: {fmt(c)}" for n, f, c in good) if good else "none"))
print("\n(3) THE SEGMENT (falling over 30 minutes, bending up) BY EACH HOUR FEATURE, halves of the feature's own pooled terciles")
S = [r for r in rows if seg(r)]
for name, fn in FEATS:
    xs = sorted(fn(r) for r in rows); cuts = [xs[len(xs) // 3], xs[2 * len(xs) // 3]]
    t3 = lambda r: 1 + sum(1 for c in cuts if fn(r) > c)
    print(f"  {name:8s} " + " | ".join(f"{t}: {fmt(cell([r for r in S if t3(r) == t]))}" for t in (1, 2, 3)))
