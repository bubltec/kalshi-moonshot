"""IS THE BURST WEIGHTED RIGHT: the moonshot's burst is the side's taker CONTRACTS in the last 3 s against that side's own median second of the 2 minutes before. A cheap side buys many more contracts per dollar as its price falls (the same $100 is 100 contracts at 50c and 3,300 at 3c),
so a side sliding from 30c to 3c would 'burst' in contracts with no extra money behind it. This recomputes each moonshot bet's burst in DOLLARS (contracts x the price they traded at) and against the price of the 2 minutes before, and compares winners with losers.
Read-only.
    docker compose exec -T bot python - < scripts/burst_weight_study.py
"""
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
rows = main.db.execute("""SELECT d.ts, d.ticker, d.side, d.ask, d.contracts, d.mode, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker
                           WHERE d.lane='crypto_moonshot' AND d.action='bet' AND w.result IN ('yes','no') ORDER BY d.ts""").fetchall()
out = []
for r in rows:
    sec0 = int(r["ts"])
    fl = {x[0]: x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (r["ticker"], sec0 - 130, sec0 + 1)).fetchall()}

    def side_vol(sec, side=r["side"]):
        x = fl.get(sec)
        if not x or x[3] is None:
            return 0.0, 0.0
        if side == "yes":
            return x[1], x[1] * x[3]
        return x[2], x[2] * (1.0 - x[3])  # NO takers pay 1 - the YES price

    c3 = d3 = 0.0
    for k in range(3):
        c, d = side_vol(sec0 - k)
        c3 += c
        d3 += d
    prior_c = sorted(side_vol(sec0 - 3 - k)[0] for k in range(120))
    prior_d = sorted(side_vol(sec0 - 3 - k)[1] for k in range(120))
    base_c = (prior_c[59] + prior_c[60]) / 2
    base_d = (prior_d[59] + prior_d[60]) / 2
    # the side's price two minutes ago against at the buy
    px = []
    for k in (120, 90, 60, 30, 0):
        x = fl.get(sec0 - k) or next((fl[t] for t in sorted(fl, reverse=True) if t <= sec0 - k), None)
        if x and x[3] is not None:
            px.append(x[3] if r["side"] == "yes" else 1.0 - x[3])
    out.append({
        "won": r["result"] == r["side"], "side": r["side"], "ratio_c": c3 / (3 * base_c + 1), "ratio_d": d3 / (3 * base_d + 0.01), "dollars_3s": d3,
        "px_then": px[0] if px else None, "px_now": r["ask"], "mode": r["mode"], "coin": r["ticker"].split("-")[0][2:5], "base_d": base_d,
    })
print(len(out), "settled moonshot bets")
for label, key in (("ratio in contracts (what the rule uses)", "ratio_c"), ("ratio in dollars", "ratio_d")):
    w = [o[key] for o in out if o["won"]]
    l = [o[key] for o in out if not o["won"]]
    print(f"{label:42} winners median {statistics.median(w):7.1f} (n={len(w)})   losers median {statistics.median(l):7.1f} (n={len(l)})")
print("\nthe side's price two minutes before the buy against at the buy (cents): winners vs losers")
for name, grp in (("winners", [o for o in out if o["won"]]), ("losers", [o for o in out if not o["won"]])):
    ps = [o for o in grp if o["px_then"] is not None]
    print(f"  {name:8} n={len(ps):2}  before {100 * statistics.median(o['px_then'] for o in ps):5.1f}c -> at the buy {100 * statistics.median(o['px_now'] for o in ps):5.1f}c;  dollars in the 3 s: median ${statistics.median(o['dollars_3s'] for o in grp):7.1f}")
print("\nby coin and side: bets, wins, median contract ratio, median DOLLAR ratio, median dollars in 3 s")
g = defaultdict(list)
for o in out:
    g[(o["coin"], o["side"])].append(o)
for (coin, side), v in sorted(g.items()):
    print(f"  {coin} {side:3}: {len(v):2} bets, {sum(o['won'] for o in v)} won, contract ratio {statistics.median(o['ratio_c'] for o in v):6.1f}, dollar ratio {statistics.median(o['ratio_d'] for o in v):6.1f}, ${statistics.median(o['dollars_3s'] for o in v):7.1f} in 3 s, normal second ${statistics.median(o['base_d'] for o in v):6.1f}")

print("\nwhat a dollar-ratio bar would have kept (all 61 settled bets; stake-normalized: each ticket scaled to $1 spent; fee included)")
from kalshi_moonshot.strategy import fee_per_contract as fee

ret = []
for o, r in zip(out, rows, strict=True):
    a = float(r["ask"])
    ret.append(((1.0 if o["won"] else 0.0) - a - fee(a)) / (a + fee(a)))
for bar in (0, 2, 3, 4, 5, 6, 8):
    keep = [(o, x) for o, x in zip(out, ret, strict=True) if o["ratio_d"] >= bar]
    if keep:
        print(f"  dollar ratio >= {bar}: {len(keep):2} bets, {sum(o['won'] for o, _ in keep):2} won, mean return per $ {100 * statistics.mean(x for _, x in keep):+7.0f}%")
print("  contract ratio (current rule) >= 5 :", f"{len(out)} bets, {sum(o['won'] for o in out)} won, mean return per $ {100 * statistics.mean(ret):+.0f}%")
