"""PriskP exit study v3 (2026-11-22; user: "it's always a trail, and sell on a drop, the drop just depends on how far in the market we are. Looking at the history if it shows a potential drop we should sell while we are up").
QUESTION: on the same model buys (scripts/priskp_run.py: minutes 2-14, all five checks read and YES, a take at the ask), does an always-on trail, whose allowed drop scales with how far into the market we are, beat holding to settlement; and does tightening it where the history before the buy showed a potential drop help? THE RULE (the trail and its scaling are the user's words; the numbers are mine and named): from the buy, track the highest bid; sell at the first 10-second reading where the bid is DROP = K x u x sqrt(minutes left) under that high, u = the side's own typical minute (root-mean-square 10-second change of the bids seen since the buy, times sqrt(6); read from 7 bids on, past bids only). So the allowed drop is one typical move over the time left: wide early, tight at the end. K = 1.0 (my choice). No share target, no other exit; otherwise hold to settlement. VARIANT B (my reading of "if the history shows a potential drop, sell while we are up"): the same trail with K = 0.5 where the history before the buy showed the pattern of the user's HYPE review (the 15-minute move up, fifth 4-5, while the 30 and 60-minute slopes were not rising, terciles 1-2); chosen after that review, a lead only. Net per $1 after both fees (0.07 p (1-p)); a quote older than 30 s is skipped; no re-buy; not covered: whether a sale at that bid would fill. SENSITIVITY K = 0.5 and 2.0 shown beside K = 1.0, NOT chosen.
Input: priskp_buys_crypto.json.gz with the bids path. Run: python3 scripts/priskp_exit_study.py priskp_buys_crypto.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = [r for r in json.load(gzip.open(sys.argv[1])) if r["rule"] == "model" and r.get("bids") is not None]
S = lambda r, k: r["states"].get(k)
flag = lambda r: S(r, "f:mv15") in (4, 5) and S(r, "t:sl30") in (1, 2) and S(r, "t:sl60") in (1, 2)


def run(r, k):
    p = r["price"]; cost = p + fee(p)
    hold = ((1.0 if r["won"] else 0.0) - cost) * 10.0 / p
    start = int(r["minute"] * 60 // 10) + 1
    high, seen = 0.0, []
    for i, b in enumerate(r["bids"]):
        if b is None: continue
        x = start + i; left = 15.0 - x / 6.0
        seen.append(b); high = max(high, b)
        if len(seen) >= 7 and left > 0:
            d = [c - a for a, c in zip(seen, seen[1:])]
            u = math.sqrt(sum(v * v for v in d) / len(d)) * math.sqrt(6.0)
            if u > 0 and b <= high - k * u * math.sqrt(left):
                return ((b - fee(b)) - cost) * 10.0 / p, hold, "trail", x / 6.0, b > p
    return hold, hold, "settled", None, None


def line(g, i):
    n = len(g)
    if n < 30: return "n %4d (too few)" % n
    sold = [r for r in g if r["x"][i][2] == "trail"]
    up = sum(1 for r in sold if r["x"][i][4])
    return "n %4d | exit %+7.0f$ %+5.1fc per $1 ahead %2.0f%% | held %+7.0f$ %+5.1fc ahead %2.0f%% | sold %2.0f%% (%2.0f%% of sales above the entry bid)" % (
        n, sum(r["x"][i][0] for r in g), 100 * sum(r["x"][i][0] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][i][0] > 0) / n,
        sum(r["x"][i][1] for r in g), 100 * sum(r["x"][i][1] for r in g) / (10.0 * n), 100 * sum(1 for r in g if r["x"][i][1] > 0) / n, 100 * len(sold) / n, 100 * up / max(1, len(sold)))


for r in rows:
    a, b, c = run(r, 1.0), run(r, 0.5), run(r, 2.0)
    bt = b if flag(r) else a  # variant B: K 0.5 where the history flag holds, else K 1.0
    r["x"] = [a, bt, b, c]
print(f"{len(rows)} model buys with a bid path; history flag (15-min up, 30 and 60-min slopes not rising) holds on {sum(1 for r in rows if flag(r))}\n")
print("A. STATED TRAIL, K 1.0: " + line(rows, 0))
print("B. TRAIL WITH THE HISTORY TIGHTENING (K 0.5 where flagged, else 1.0): " + line(rows, 1))
print("   sensitivity, not chosen: K 0.5 everywhere: " + line(rows, 2) + "\n                            K 2.0 everywhere: " + line(rows, 3))
print("\nBY BUY MINUTE (A)")
for a, b in ((2, 4), (4, 5), (5, 7), (7, 10), (10, 12), (12, 14.01)): print(f"  min {a}-{int(b)}: " + line([r for r in rows if a <= r["minute"] < b], 0))
print("\nBY PRICE PAID (A)")
for a, b in ((0.0, 0.45), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.0)): print(f"  {a:.2f}-{b:.2f}: " + line([r for r in rows if a <= r["price"] < b], 0))
fl = [r for r in rows if flag(r)]
print("\nTHE FLAGGED BUYS ALONE: A " + line(fl, 0) + "\n                        B " + line(fl, 1))
print("\nBY 30-MINUTE SLOPE x CURVATURE TERCILES (A)")
for a in (1, 2, 3):
    for c in (1, 2, 3): print(f"  slope {a} curv {c}: " + line([r for r in rows if S(r, "t:sl30") == a and S(r, "t:cv30") == c], 0))
