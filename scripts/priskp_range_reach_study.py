"""Is the target inside the recent swing's range? (2026-11-25; user, on BTC 08:30 NO bought at the 8:20 trough after a spike that had reached the target five minutes earlier: "how did it not pick up that wave before?"; "the range on the curve needs to be flexible".)
QUESTION (one): on the live priskp bets of the 24 hours to 11-25 10:20 ET, at the buy, the target's distance from the spot on the bet's winning side divided by the spot's PEAK-TO-TROUGH range over the 15 minutes before the buy (a wave's own travel, not a standard deviation, which a short spike barely moves). Bets by that ratio: under 0.75 (the target is inside the recent range), 0.75 to 1.5, 1.5 and over: n, lost, as-run net. The 08:30 round listed.
HONEST LIMITS: 124 bets in 24 hours, one regime; the 15 minutes and the cuts 0.75 and 1.5 were fixed before the run, nothing else tried; 'as run' mixes the lane's exits with the entry; a lead only. A rule on it goes to shadow first.
INPUT: bets24.json, targets24.json, d_<index>.raw (10 s averages) in the directory given.
Run on this Mac: uv run python scripts/priskp_range_reach_study.py <dir>"""
import bisect
import json
import re
import sys

D = sys.argv[1]
IDX = {"KXBNB15M": "BNBUSD_RTI", "KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXHYPE15M": "HYPEUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI", "KXZEC15M": "ZECUSD_RTI", "KXNEAR15M": "NEARUSD_RTI"}


def fee(p):
    return 0.07 * p * (1 - p)


def load(ix):
    line = [x.strip() for x in open(f"{D}/d_{ix}.raw").read().split("\n") if re.match(r"^\d+:[\d.]+,", x.strip())][0]
    t, v = [], []
    for a, b in (p.split(":") for p in line.split(",")):
        t.append(int(a))
        v.append(float(b))
    return t, v


spot = {ix: load(ix) for ix in set(IDX.values())}
targets = json.load(open(f"{D}/targets24.json"))
rows = []
for b in json.load(open(f"{D}/bets24.json")):
    ix = IDX.get(b["ticker"].split("-")[0])
    K = (targets.get(b["ticker"]) or [None])[0]
    if ix is None or not K:
        continue
    t, v = spot[ix]
    hi = bisect.bisect_right(t, b["ts"])
    w = v[bisect.bisect_left(t, b["ts"] - 900):hi]
    if len(w) < 60:
        continue
    rng = max(w) - min(w)
    if rng <= 0:
        continue
    dist = (K - w[-1]) if b["side"] == "no" else (w[-1] - K)
    pay, n = float(b["pay"]), float(b["n"])
    cost = pay + fee(pay)
    got = (float(b["xp"]) - fee(float(b["xp"]))) if b["xts"] else (1.0 if b["out"] == b["side"] else 0.0)
    rows.append({"tk": b["ticker"], "side": b["side"], "ratio": dist / rng, "net": (got - cost) * n, "pc": got - cost})


def line(label, g):
    if not g:
        print(f"{label:60s} n   0")
        return
    lost = sum(1 for x in g if x["pc"] <= 0)
    print(f"{label:60s} n {len(g):3d}  as run ${sum(x['net'] for x in g):+8.2f}  lost {lost:3d} ({100 * lost / len(g):.0f}%)  per contract {sum(x['pc'] for x in g) / len(g):+.3f}")


print(f"{len(rows)} bets")
line("all", rows)
line("target within 0.75 of the last 15 minutes' range (inside the swing)", [x for x in rows if x["ratio"] < 0.75])
line("0.75 to 1.5 ranges away", [x for x in rows if 0.75 <= x["ratio"] < 1.5])
line("1.5 ranges or more away", [x for x in rows if x["ratio"] >= 1.5])
print("\nthe 08:30 round:")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} target {x['ratio']:.2f} ranges away, net ${x['net']:+.2f}")

print("\nPOST-HOC finer cuts (chosen after seeing the 08:30 ratios of 0.36 to 0.62; not part of the fixed question):")
for lo, hi in ((0.0, 0.4), (0.4, 0.55), (0.55, 0.65), (0.65, 0.75)):
    line(f"  target {lo:.2f} to {hi:.2f} ranges away", [x for x in rows if lo <= x["ratio"] < hi])
