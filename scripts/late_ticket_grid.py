"""Your rule, as a grid: a side under 40 cents, the feed within X of the strike, and a 5/15/60-minute trend filter.

Every combination is tuned on the earliest 60% of windows and judged on the latest 40%, with a block bootstrap
(whole windows) on the held-out ROI. Needs the dump from late_ticket_study.py:
    python scripts/late_ticket_study.py moonshot.db 0.01 0.40 DIR/late_rows.json
    python scripts/late_ticket_grid.py DIR [max_price]
"""
import itertools, json, random, sys
DIR = sys.argv[1]; MAXP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.40
UNIT = sys.argv[3] if len(sys.argv) > 3 else "pct"    # "pct": distance as a share of price; "sigma": in the instrument's own expected moves
rows = [r for r in json.load(open(DIR + "/late_rows.json")) if r["price"] < MAXP and r["tr60"] is not None and r["tr5"] is not None and r["tr15"] is not None]
fee = lambda p: 0.07 * p * (1 - p)
def roi(g):
    c = sum(r["price"] + fee(r["price"]) for r in g)
    return sum(r["won"] for r in g) / c - 1 if c else float("nan")
cut = rows[int(len(rows) * 0.6)]["t"]
train = [r for r in rows if r["t"] < cut]; test = [r for r in rows if r["t"] >= cut]
print(f"{len(rows)} entries under {MAXP:.2f} ({len(train)} train, {len(test)} test); buying all: train {roi(train):+.1%}, test {roi(test):+.1%}\n")
DIST = (0.0005, 0.001, 0.002, 0.003, 0.005, 0.01) if UNIT == "pct" else (0.1, 0.25, 0.5, 0.75, 1.0, 1.5)
MODES = ("any", "toward", "away")
def keep(r, d, m5, m15, m60):
    if abs(r["dist"] if UNIT == "pct" else r["need"]) > d: return False
    for mode, key in ((m5, "tr5"), (m15, "tr15"), (m60, "tr60")):
        if mode == "toward" and not r[key] > 0: return False
        if mode == "away" and not r[key] < 0: return False
    return True
res = []
for d, a, b, c in itertools.product(DIST, MODES, MODES, MODES):
    tr = [r for r in train if keep(r, d, a, b, c)]; te = [r for r in test if keep(r, d, a, b, c)]
    if len(tr) >= 30 and len(te) >= 15: res.append((roi(tr), roi(te), len(tr), len(te), d, a, b, c, te))
res.sort(key=lambda x: -x[0])
print(f"{len(res)} settings with enough entries. Best on the training days:\n")
print(f"{'within':>8} {'5m':>7} {'15m':>7} {'1h':>7} | {'train n':>7} {'ROI':>7} | {'test n':>6} {'ROI':>7}  90% range (test)")
random.seed(3)
for tr_roi, te_roi, ntr, nte, d, a, b, c, te in res[:10]:
    by = {}
    for r in te: by.setdefault((r["series"], r["t"]), []).append(r)
    ks = list(by); bs = sorted(roi([x for k in random.choices(ks, k=len(ks)) for x in by[k]]) for _ in range(300))
    print(f"{(format(d, ".2%") if UNIT == "pct" else format(d, ".2f") + " sig"):>8} {a:>7} {b:>7} {c:>7} | {ntr:>7} {tr_roi:>+7.1%} | {nte:>6} {te_roi:>+7.1%}  [{bs[15]:+.0%}, {bs[285]:+.0%}]")
pos = sum(1 for x in res if x[1] > 0)
print(f"\nOf {len(res)} settings, {pos} were positive on the test days; the median test ROI was {sorted(x[1] for x in res)[len(res)//2]:+.1%}.")
print("Settings tried on the same data inflate the best-looking ones: treat the top rows as leads, not results.")
