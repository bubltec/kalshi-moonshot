"""A cap on the chance the lane will bet on (lane field max_chance, live default 1.0) and how it would have saved money (2026-11-15, user: "Let's look at a cap first and how this would have saved money").
Same bets: every live crypto_multiscale buy since Mon 2026-11-09 00:00 ET (the logic was rewritten then) that has a claimed chance (decisions.model_p, the side's own chance, the estimator's own: runner.chance_ok reads it before calibration; record adjustment is off on this lane so they are the same) and a result (its own exit with the sale fee, or settlement; fee on every leg). For each cap: the buys it would have skipped (claimed chance over the cap), what they made, split into the losers it avoids and the winners it gives up, and where the losers sit in time. A skipped buy is simply not made; the walk does not replace it with a later buy in the same market, which the lane might have made at a lower claim. Said plainly: in-sample, 16 buys at or over 0.95, five of the losers in two market-wide spikes.
Run on the host: python scripts/max_chance_cap_study.py"""
import collections
import datetime
import json
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT ts, ticker, side, model_p, contracts, stake_usd, exit_price, exit_reason, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND model_p IS NOT NULL ORDER BY ts", (SINCE,)).fetchall()
cache = {}


def res(t, o):
    if o in ("yes", "no"):
        return o
    if t not in cache:
        try:
            cache[t] = json.load(urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=10))["market"].get("result")
        except Exception:
            cache[t] = None
    return cache[t]


bets = []
for r in rows:
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        v = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
        how = "take" if "profit take" in (r["exit_reason"] or "") else "stop" if "max loss" in (r["exit_reason"] or "") else "exit"
    else:
        rr = res(r["ticker"], r["outcome"])
        if rr not in ("yes", "no"):
            continue
        v = (n if rr == r["side"] else 0.0) - cost; how = "held won" if v > 0 else "held lost"
    bets.append(dict(t=float(r["ts"]), p=float(r["model_p"]), v=v, cost=cost, how=how, tk=r["ticker"][2:-3], side=r["side"]))
print(f"live buys since Mon 2026-11-09 with a chance and a result: {len(bets)}; net {sum(b['v'] for b in bets):+.2f} on ${sum(b['cost'] for b in bets):.0f} staked")
print("\ncap on the claimed chance: buys skipped | what they made | losers avoided | winners given up | saved by the cap")
for cap in (0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.90):
    sk = [b for b in bets if b["p"] > cap + 1e-9]
    lo = [b for b in sk if b["v"] <= 0]; wi = [b for b in sk if b["v"] > 0]
    print(f"  cap {cap:.2f}: skips {len(sk):3d} | made {sum(b['v'] for b in sk):+8.2f} | {len(lo):2d} losers {sum(b['v'] for b in lo):+8.2f} | {len(wi):2d} winners {sum(b['v'] for b in wi):+8.2f} | saved {-sum(b['v'] for b in sk):+8.2f} ({-sum(b['v'] for b in sk) / max(sum(b['cost'] for b in sk), 1e-9) * 100:+.0f}% of what those buys staked)")
print("\ncap 0.95: where the skipped buys sit (by hour of the buy, ET)")
g = collections.OrderedDict()
for b in bets:
    if b["p"] > 0.95 + 1e-9:
        k = datetime.datetime.fromtimestamp(b["t"], ET).strftime("%m-%d %H:00")
        e = g.setdefault(k, [0, 0.0, []]); e[0] += 1; e[1] += b["v"]; e[2].append(f"{b['tk']} {b['side']} {b['p']:.3f} {b['v']:+.2f} {b['how']}")
for k, (n, v, lst) in g.items():
    print(f"  {k}: {n} buys {v:+.2f}: " + "; ".join(lst))
print("DONE")
