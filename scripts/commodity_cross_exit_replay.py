"""The PriskP cross exit on the commodity lanes' own live bets (2026-11-24; user: the commodity lanes "need a better exit", "we can't use priskp?").
QUESTION (one, on the same bets): on every settled live bet of the commodity lanes (11-12 on), does selling at the bid the first time the perp's 60 s average is on the wrong side of the target (checked every 5 s; target = the perp's value at the window's open) beat what the lane's own exit did and holding to settlement? Fee on every leg (0.07 p (1-p)). A sale is at the recorded bid of that second; no fill is modelled. Split by the bet's own typical-minute size u (rms one-second change of the side's mid over the 900 s before the buy x sqrt(60)), thirds of this sample: a curve-size cut, never an asset. Reported: n, net $, bets beat and lost, winners sold. A lead only, read-only; the target is the perp at the open, not Kalshi's own commodity strike.
Run: PYTHONPATH=src .venv/bin/python scripts/commodity_cross_exit_replay.py comm_lines.txt (from scripts/commodity_lane_dump.py on the host)."""
import json
import math
import sys


def fee(p):
    return 0.07 * p * (1.0 - p)


recs = []
for line in open(sys.argv[1]):
    if line.startswith("DUMP"):
        try:
            recs.append(json.loads(line[4:]))
        except ValueError:
            pass
res, skipped = [], 0
for rec in recs:
    q, ticks = rec["quotes"], {int(a): float(b) for a, b in rec["ticks"]}
    for b in rec["bets"]:
        t0, close, n = float(b["ts"]), float(b["close_ts"]), float(b["contracts"])
        basis, side = float(b["stake_usd"]) / n, b["side"]
        opened = [v for k, v in sorted(ticks.items()) if close - 900 <= k <= close - 840]
        if not opened or len(q) < 60 or not 0.02 < basis < 0.99:
            skipped += 1
            continue
        strike = opened[0]
        sb = [(x[0], x[1] if side == "yes" else 1.0 - x[2]) for x in q]
        grid, j = [], 0
        for sec in range(int(t0 - 900), int(close - 1)):
            while j + 1 < len(sb) and sb[j + 1][0] <= sec:
                j += 1
            grid.append((sec, sb[j][1]))
        pre = [g[1] for g in grid if g[0] < t0]
        dd = [pre[i + 1] - pre[i] for i in range(len(pre) - 1)]
        if len(dd) < 120:
            skipped += 1
            continue
        u = math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0)
        won = b["result"] == side
        held = (1.0 if won else 0.0) - basis - fee(basis)
        sold = b["exit_price"] is not None and b["exit_ts"] is not None and float(b["exit_ts"]) < close - 1
        actual = ((float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)) - basis - fee(basis)

        def cush(sec, ticks=ticks, strike=strike, side=side):
            vals = [ticks[k] for k in range(sec - 59, sec + 1) if k in ticks]
            if len(vals) < 20:
                return None
            a = sum(vals) / len(vals)
            return (a - strike) if side == "yes" else (strike - a)

        cross = held
        for sec, bid in grid:
            if sec <= t0 or sec % 5:
                continue
            c = cush(sec)
            if c is not None and c <= 0:
                cross = bid - fee(bid) - basis - fee(basis)
                break
        res.append({"n": n, "u": u, "won": won, "hold": held, "actual": actual, "cross": cross})
us = sorted(r["u"] for r in res)
cuts = [us[len(us) // 3], us[2 * len(us) // 3]]
print(f"{len(res)} bets (left out: {skipped}), lost {sum(1 for r in res if not r['won'])}; u thirds cut at {cuts[0]:.3f}, {cuts[1]:.3f}")


def show(lab, g):
    print(f"-- {lab}: {len(g)} bets, staked ${sum(r['n'] * 0 for r in g):.0f}")
    for k in ("hold", "actual", "cross"):
        net = sum(r[k] * r["n"] for r in g)
        print(f"  {k:7s} net {net:+8.2f} | cross-vs: n/a" if k != "cross" else f"  {k:7s} net {net:+8.2f} | beat/lost to what ran {sum(1 for r in g if r['cross'] > r['actual'] + 1e-9)}/{sum(1 for r in g if r['cross'] < r['actual'] - 1e-9)} | beat/lost to hold {sum(1 for r in g if r['cross'] > r['hold'] + 1e-9)}/{sum(1 for r in g if r['cross'] < r['hold'] - 1e-9)} | winners sold {sum(1 for r in g if r['won'] and abs(r['cross'] - r['hold']) > 1e-9)}")


show("all", res)
show("small u", [r for r in res if r["u"] <= cuts[0]])
show("middle u", [r for r in res if cuts[0] < r["u"] <= cuts[1]])
show("large u", [r for r in res if r["u"] > cuts[1]])
