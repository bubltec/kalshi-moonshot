"""PriskP exit study v2 (2026-11-22; user: "A share of the way to $1, before 3 mins remaining. We can always rebuy. And after 3 mins remaining a drop from the high").
QUESTION: on the same model buys (scripts/priskp_run.py: minutes 2-14, all five checks read and YES, a take at the ask), does this exit beat holding to settlement? THE RULE (the two parts are the user's words; the numbers are mine and named): MORE THAN 3 MINUTES LEFT (before minute 12 of the 15): sell at the first 10-second reading where the side's bid has covered SHARE (0.5, my choice, the user gave no number) of the way from the entry price to $1.00. 3 MINUTES LEFT OR LESS (minute 12 on): the share rule is off; sell when the bid falls DROP (1.0, my choice) typical minutes of the side's own bid below the highest bid since the buy (the typical minute: root-mean-square 10-second change of the bids seen so far since the buy, times sqrt(6); read from 6 bids on, from past bids only). Otherwise hold to settlement. Net per $1 after both fees (0.07 p (1-p)); a quote older than 30 s is skipped; no re-buy here (the user: we can always rebuy; not studied); not covered: whether a sale at that bid would fill. SENSITIVITY lines at SHARE 0.33 and 0.67 are shown beside the stated 0.5 and were NOT chosen.
Input: priskp_buys_crypto.json.gz with the bids path. Run: python3 scripts/priskp_exit_study.py priskp_buys_crypto.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = [r for r in json.load(gzip.open(sys.argv[1])) if r["rule"] == "model" and r.get("bids") is not None]
DROP = 1.0


def run(r, share):
    p = r["price"]; cost = p + fee(p)
    hold = ((1.0 if r["won"] else 0.0) - cost) * 10.0 / p
    start = int(r["minute"] * 60 // 10) + 1
    high, seen = 0.0, []
    for k, b in enumerate(r["bids"]):
        if b is None: continue
        x = start + k; left = 15.0 - x / 6.0
        seen.append(b); high = max(high, b)
        if left > 3.0:
            if b >= p + share * (1.0 - p) - 1e-9: return ((b - fee(b)) - cost) * 10.0 / p, hold, "share", x / 6.0
        elif len(seen) >= 7:
            d = [c - a for a, c in zip(seen, seen[1:])]
            u = math.sqrt(sum(v * v for v in d) / len(d)) * math.sqrt(6.0)
            if u > 0 and b <= high - DROP * u: return ((b - fee(b)) - cost) * 10.0 / p, hold, "drop", x / 6.0
    return hold, hold, "settled", None


def line(g, i):
    n = len(g)
    if n < 30: return "n %4d (too few)" % n
    by = {}
    for r in g: by[r["x"][i][2]] = by.get(r["x"][i][2], 0) + 1
    return "n %4d | exit %+7.0f$ %+5.1fc per $1 ahead %2.0f%% | held %+7.0f$ %+5.1fc ahead %2.0f%% | ended: %s" % (
        n, sum(r["x"][i][0] for r in g), 100 * sum(r["x"][i][0] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][i][0] > 0) / n,
        sum(r["x"][i][1] for r in g), 100 * sum(r["x"][i][1] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][i][1] > 0) / n, " ".join(f"{k} {v}" for k, v in sorted(by.items())))


for r in rows: r["x"] = [run(r, s) for s in (0.5, 0.33, 0.67)]
print(f"{len(rows)} model buys with a bid path\n\nSTATED RULE (share 0.5, drop 1.0 typical minute):\n  ALL: " + line(rows, 0))
print("  sensitivity, not chosen: share 0.33: " + line(rows, 1) + "\n                           share 0.67: " + line(rows, 2))
print("\nBY BUY MINUTE (stated rule)")
for a, b in ((2, 4), (4, 5), (5, 7), (7, 10), (10, 12), (12, 14.01)): print(f"  min {a}-{int(b)}: " + line([r for r in rows if a <= r["minute"] < b], 0))
print("\nBY PRICE PAID (stated rule)")
for a, b in ((0.0, 0.45), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.0)): print(f"  {a:.2f}-{b:.2f}: " + line([r for r in rows if a <= r["price"] < b], 0))
print("\nBY 30-MINUTE SLOPE x CURVATURE TERCILES (stated rule)")
for a in (1, 2, 3):
    for c in (1, 2, 3): print(f"  slope {a} curv {c}: " + line([r for r in rows if r["states"].get("t:sl30") == a and r["states"].get("t:cv30") == c], 0))
sold = [r for r in rows if r["x"][0][2] != "settled"]
t = sorted(r["x"][0][3] - r["minute"] for r in sold)
print(f"\nSOLD {len(sold)} of {len(rows)}; median minutes from buy to sale {t[len(t) // 2]:.1f}; of those sold, held would have WON {100 * sum(1 for r in sold if r['won']) / len(sold):.0f}%")
