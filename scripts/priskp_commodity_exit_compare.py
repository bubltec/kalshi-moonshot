"""PriskP buys on the commodity series, three exits on the same bets (2026-11-24; user: "would the priskp work on commodities? I kind of want to replace the 4 commodity lanes model"; "it doesn't exit like the priskp lane").
QUESTION (one): on every buy the PriskP checks model makes on the commodity 15-minute series (priskp_run.py SERIES=commodity: five checks all in fifths 4-5, minutes 2-14, never an ask of 0.45-0.55, a take at the ask, target = the Kalshi perp's value at the window's open), which exit pays most after the fee on every leg (0.07 p (1-p)): hold to settlement, the PriskP cross exit, or a commodity-style exit?
EXITS: HOLD. CROSS = sell at the bid the first 10 s mark where the perp's 60 s average is on the wrong side of the target (the live PriskP exit). COMMODITY-LIKE (an approximation of exit.commodity_exit, not the real step code): TRAIL = once the bid after both fees is ahead of the cost, sell when the bid is 1.0 u under its high since the buy (u = rms of the 10 s bid changes so far x sqrt(6)); TIME = from 240 s before the close, sell when the bid is under 0.5. TRAIL+TIME = whichever first. BOTH = CROSS or TRAIL, whichever first. A sale is at the recorded bid of that 10 s mark; no fill is modelled. $10 a bet. Split by the bet's own bid-path size u (thirds), never by market name. Lead only; read-only.
Run: .venv/bin/python scripts/priskp_commodity_exit_compare.py priskp_buys_commodity.json.gz"""
import gzip
import json
import math
import sys


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = [r for r in json.loads(gzip.decompress(open(sys.argv[1], "rb").read())) if r["rule"] == "model" and r.get("bids")]
STAKE = 10.0
res = []
for r in rows:
    p = r["price"]
    sh = STAKE / p
    bids, cush = r["bids"], r.get("cross") or []
    x0 = int(r["minute"] * 6) + 1  # the first mark after the buy, 10 s each, from the window's open
    cost = p + fee(p)
    hold = r["net"]

    def pay(b):
        return (b - fee(b) - cost) * sh

    def run(cross, trail, ttg):
        high, ahead, diffs, last = None, False, [], None
        for i, b in enumerate(bids):
            if b is None:
                continue
            x = x0 + i
            if last is not None:
                diffs.append(b - last)
            last = b
            high = b if high is None else max(high, b)
            if cross and i < len(cush) and cush[i] is not None and cush[i] <= 0:
                return pay(b)
            if trail:
                if b - fee(b) > cost:
                    ahead = True
                if ahead and len(diffs) >= 6:
                    u = math.sqrt(sum(d * d for d in diffs) / len(diffs)) * math.sqrt(6.0)
                    if b <= high - 1.0 * u:
                        return pay(b)
            if ttg and 900 - 10 * x <= 240 and b < 0.5:
                return pay(b)
        return hold

    d = [b - a for a, b in zip([v for v in bids if v is not None], [v for v in bids if v is not None][1:], strict=False)]
    u = math.sqrt(sum(v * v for v in d) / len(d)) * math.sqrt(6.0) if d else 0.0
    res.append({"u": u, "won": r["won"], "hold": hold, "cross": run(True, False, False), "trail": run(False, True, False), "trailtime": run(False, True, True), "both": run(True, True, False), "series": r["series"]})
print(f"{len(res)} model buys with a bid path; {len({r['series'] for r in res})} series")
keys = ["hold", "cross", "trail", "trailtime", "both"]
us = sorted(r["u"] for r in res)
cuts = [us[len(us) // 3], us[2 * len(us) // 3]] if len(us) >= 6 else [0, 0]


def show(lab, g):
    if not g:
        return
    print(f"-- {lab}: {len(g)} bets, won {sum(1 for r in g if r['won'])}, staked ${STAKE * len(g):.0f}")
    for k in keys:
        net = sum(r[k] for r in g)
        print(f"  {k:9s} net {net:+8.2f} | beat/lost to hold {sum(1 for r in g if r[k] > r['hold'] + 1e-9):3d}/{sum(1 for r in g if r[k] < r['hold'] - 1e-9):3d} | sold bets that won {sum(1 for r in g if r['won'] and abs(r[k] - r['hold']) > 1e-9):3d}")


show("all", res)
show("small u", [r for r in res if r["u"] <= cuts[0]])
show("middle u", [r for r in res if cuts[0] < r["u"] <= cuts[1]])
show("large u", [r for r in res if r["u"] > cuts[1]])
