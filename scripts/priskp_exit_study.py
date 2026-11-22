"""PriskP exit study, one rule (2026-11-22; user: early buys are not held to settlement, a fee-free profit check; refine entry and exit on this fresh run).
QUESTION: on the same model buys (scripts/priskp_run.py, minutes 2-14, all five checks read and YES, a take at the ask), does selling at the first moment the side's bid, after its fee, beats what the buy cost, after its fee, beat holding to settlement? RULE (stated before the run, not tuned): sell at the first 10-second reading after the buy where bid - fee(bid) > price + fee(price) (any profit after both fees); otherwise hold to settlement. Net per $1 staked after both fees, the share of buys that sold, the share ahead, and the same buys held. A quote older than 30 s is skipped. Not covered: whether a sale at that bid would fill (queue, size).
Report: all buys, by buy-minute band, by price paid, by wave-shape tercile (30-minute slope x curvature, the run's own terciles). Input: priskp_buys_crypto.json.gz written with the bids path. Run: python3 scripts/priskp_exit_study.py priskp_buys_crypto.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = [r for r in json.load(gzip.open(sys.argv[1])) if r["rule"] == "model" and r.get("bids") is not None]


def run(r):
    cost = r["price"] + fee(r["price"])
    hold = ((1.0 if r["won"] else 0.0) - cost) * 10.0 / r["price"]
    for k, b in enumerate(r["bids"]):
        if b is not None and b - fee(b) > cost + 1e-9:
            return ((b - fee(b)) - cost) * 10.0 / r["price"], hold, True, k
    return hold, hold, False, None


for r in rows: r["x"] = run(r)


def line(g):
    n = len(g)
    if n < 30: return "n %4d (too few)" % n
    sold = sum(1 for r in g if r["x"][2])
    return "n %4d | with the exit %+7.0f$ %+5.1fc per $1 ahead %2.0f%% (sold %2.0f%%) | held %+7.0f$ %+5.1fc ahead %2.0f%%" % (
        n, sum(r["x"][0] for r in g), 100 * sum(r["x"][0] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][0] > 0) / n, 100 * sold / n,
        sum(r["x"][1] for r in g), 100 * sum(r["x"][1] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][1] > 0) / n)


print(f"{len(rows)} model buys with a bid path\n\nALL: " + line(rows))
print("\nBY BUY MINUTE")
for a, b in ((2, 4), (4, 5), (5, 7), (7, 10), (10, 12), (12, 14.01)): print(f"  min {a}-{int(b)}: " + line([r for r in rows if a <= r["minute"] < b]))
print("\nBY PRICE PAID")
for a, b in ((0.0, 0.45), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.0)): print(f"  {a:.2f}-{b:.2f}: " + line([r for r in rows if a <= r["price"] < b]))
print("\nBY 30-MINUTE SLOPE x CURVATURE TERCILES (side view)")
for a in (1, 2, 3):
    print(f"  slope {a}: " + " | ".join(f"curv {c}: " + line([r for r in rows if r["states"].get("t:sl30") == a and r["states"].get("t:cv30") == c]) for c in (1, 2, 3)))
sold = [r for r in rows if r["x"][2]]
print(f"\nSOLD: median time to the sale {sorted(r['x'][3] for r in sold)[len(sold) // 2] * 10 if sold else 0} s after the buy; sold buys that settle as winners if held: {100 * sum(1 for r in sold if r['won']) / max(1, len(sold)):.0f}%")
