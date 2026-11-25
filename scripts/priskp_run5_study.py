"""Does buying right after the side's own fresh run lose? (2026-11-25; user, on HYPE and BTC 08:30 NO bought mid-fall after a ridge: "it bought into the fall".)
QUESTION (one): on the live priskp bets of the 24 hours to 11-25 10:20 ET, at the buy, how far the bet's side had RUN in the last 5 minutes from the spot's extreme against it (run5: for NO the drop from the 5 minute high, for YES the rise from the 5 minute low), in the spot's typical 10 s moves (the rms 10 s change over the 30 minutes before the buy): bets by run5 under 2, 2 to 3.5, 3.5 to 5, 5 and over: n, lost, as-run net. The 08:30 round listed.
HONEST LIMITS: 131 bets in 24 hours, one regime; the cuts 2, 3.5 and 5 are chosen after seeing the 08:30 run5 values of 3.9 to 6.0 (the user's chart led here), so they are NOT out-of-sample; 'as run' mixes the lane's exits with the entry; a lead only.
INPUT: bets24.json, d_<index>.raw in the directory given.
Run on this Mac: uv run python scripts/priskp_run5_study.py <dir>"""
import bisect
import json
import math
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
rows = []
for b in json.load(open(f"{D}/bets24.json")):
    ix = IDX.get(b["ticker"].split("-")[0])
    if ix is None:
        continue
    t, v = spot[ix]
    hi = bisect.bisect_right(t, b["ts"])
    w = v[bisect.bisect_left(t, b["ts"] - 1800):hi]
    if len(w) < 120:
        continue
    d = [y - x for x, y in zip(w, w[1:])]
    u10 = math.sqrt(sum(x * x for x in d) / len(d)) or 1e-12
    five = w[-30:]
    run = (w[-1] - min(five)) if b["side"] == "yes" else (max(five) - w[-1])
    pay, n = float(b["pay"]), float(b["n"])
    cost = pay + fee(pay)
    got = (float(b["xp"]) - fee(float(b["xp"]))) if b["xts"] else (1.0 if b["out"] == b["side"] else 0.0)
    rows.append({"tk": b["ticker"], "side": b["side"], "run5": run / u10, "net": (got - cost) * n, "pc": got - cost})


def line(label, g):
    if not g:
        print(f"{label:46s} n   0")
        return
    lost = sum(1 for x in g if x["pc"] <= 0)
    print(f"{label:46s} n {len(g):3d}  as run ${sum(x['net'] for x in g):+8.2f}  lost {lost:3d} ({100 * lost / len(g):.0f}%)  per contract {sum(x['pc'] for x in g) / len(g):+.3f}")


print(f"{len(rows)} bets")
line("all", rows)
for lo, hi in ((0, 2), (2, 3.5), (3.5, 5), (5, 99)):
    line(f"run5 {lo:g} to {hi:g} typical 10 s moves", [x for x in rows if lo <= x["run5"] < hi])
print("\nthe 08:30 round:")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} run5 {x['run5']:.2f}, net ${x['net']:+.2f}")
