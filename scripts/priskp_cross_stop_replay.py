"""PriskP cross exit plus a price-collapse stop (2026-11-24; user, on the live SOL 19:45 NO loss: bought 0.81, the bid fell to 0.28 over 3 minutes while the perp had not crossed, sold 0.41 at the cross, -$2.67).
QUESTION (one, on the same bets): does adding a stop in the bet's own units (sell when the bid is K x u under the price paid) to the live cross-only exit lose fewer dollars than the cross alone, without selling bets that went on to win? Fee on every leg (0.07 p (1-p)).
RULES (stated before the run): CROSS = the live exit: first time (checked every 5 s) the perp's 60 s average is on the wrong side of the target (the perp's value at the window's open, a tick in the first 60 s); sells at the bid. STOP K = also sell the first time the bid is K x u or more under the price paid, u = the side's typical minute (rms one-second change of the mid over the 900 s before the buy x sqrt(60)). K = 1, 2, 3 shown, none chosen. Compared on the same bets with holding. A sale is at the recorded bid; a fill is not modelled. Split by the bet's own u (thirds of this sample, a curve-size cut, never an asset). A lead only; read-only.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_cross_stop_replay.py all_lines.txt (made by scripts/priskp_dump_all.py on the host)."""
import json
import math
import sys


def fee(p):
    return 0.07 * p * (1.0 - p)


recs = [json.loads(line[4:]) for line in open(sys.argv[1]) if line.startswith("DUMP")]
KS = (1.0, 2.0, 3.0)
res, skipped = [], 0
for rec in recs:
    q, ticks = rec["quotes"], {int(a): float(b) for a, b in rec["ticks"]}
    for b in rec["bets"]:
        t0, close, n = float(b["ts"]), float(b["close_ts"]), float(b["contracts"])
        basis, side = float(b["stake_usd"]) / n, b["side"]
        opened = [v for k, v in sorted(ticks.items()) if close - 900 <= k <= close - 840]
        if not opened or len(q) < 60:
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

        def cush(sec, ticks=ticks, strike=strike, side=side):
            vals = [ticks[k] for k in range(sec - 59, sec + 1) if k in ticks]
            if len(vals) < 20:
                return None
            a = sum(vals) / len(vals)
            return (a - strike) if side == "yes" else (strike - a)

        def run(k, basis=basis, t0=t0, u=u, grid=grid, held=held):
            for sec, bid in grid:
                if sec <= t0:
                    continue
                net = bid - fee(bid) - basis - fee(basis)
                if k and bid <= basis - k * u:
                    return net
                if sec % 5 == 0:
                    c = cush(sec)
                    if c is not None and c <= 0:
                        return net
            return held

        out = {"n": n, "won": won, "hold": held, "u": u, "ticker": b["ticker"], "mode": b.get("mode"), "cross": run(0)}
        for k in KS:
            out[f"stop{k}"] = run(k)
        res.append(out)
us = sorted(r["u"] for r in res)
cuts = [us[len(us) // 3], us[2 * len(us) // 3]]
print(f"bets {len(res)} (left out: {skipped}); held {sum(r['hold'] * r['n'] for r in res):+.2f}; lost {sum(1 for r in res if not r['won'])}; u thirds cut at {cuts[0]:.3f}, {cuts[1]:.3f}")
keys = ["cross"] + [f"stop{k}" for k in KS]


def show(lab, g):
    print(f"-- {lab}: {len(g)} bets, held {sum(r['hold'] * r['n'] for r in g):+.2f}")
    for key in keys:
        net = sum(r[key] * r["n"] for r in g)
        vs = [r for r in g if abs(r[key] - r["cross"]) > 1e-9]
        print(f"  {key:8s} net {net:+7.2f} | beat/lost to cross {sum(1 for r in g if r[key] > r['cross'] + 1e-9):2d}/{sum(1 for r in g if r[key] < r['cross'] - 1e-9):2d} | beat/lost to hold {sum(1 for r in g if r[key] > r['hold'] + 1e-9):2d}/{sum(1 for r in g if r[key] < r['hold'] - 1e-9):2d} | sold bets that won {sum(1 for r in g if r['won'] and r[key] != r['hold']):2d} | changed {len(vs)}")


show("all", res)
show("small u (lowest third)", [r for r in res if r["u"] <= cuts[0]])
show("middle u", [r for r in res if cuts[0] < r["u"] <= cuts[1]])
show("large u (top third)", [r for r in res if r["u"] > cuts[1]])
