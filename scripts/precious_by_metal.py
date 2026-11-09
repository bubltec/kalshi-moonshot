"""Why do platinum and palladium wins pay less than gold and silver? (2026-11-09, user). Per series on the commodity favorite lanes since 11-01 (platinum and palladium are in commodity_industrial): bets, win rate, average price paid, stake, win $, loss $, fee paid per bet, net per $1. Review only.
    docker compose exec -T bot python - < scripts/precious_by_metal.py
"""
import collections, time
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
since = time.time() - 9 * 86400
rows = st.db.execute("""SELECT d.ticker,d.side,d.ask,d.contracts,d.stake_usd,d.exit_price,w.result,w.series FROM decisions d JOIN windows w ON w.ticker=d.ticker
  WHERE d.action='bet' AND d.mode='trade' AND d.ts>? AND d.lane IN ('commodity_precious','commodity_industrial','commodity_energy','commodity_wti','commodity_natgas') AND w.result IN ('yes','no') AND d.contracts>0""", (since,)).fetchall()
a = collections.defaultdict(lambda: dict(n=0, won=0, ask=0.0, stake=0.0, win=0.0, loss=0.0, fee=0.0, net=0.0, nw=0, nl=0, ctr=0.0))
for r in rows:
    n, c, ask = float(r["contracts"]), float(r["stake_usd"]), float(r["ask"])
    net = (n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) if r["exit_price"] else (n if r["result"] == r["side"] else 0.0)) - c
    x = a[r["series"]]
    x["n"] += 1; x["ask"] += ask; x["stake"] += c; x["net"] += net; x["fee"] += n * fee(ask); x["ctr"] += n
    if net > 0: x["nw"] += 1; x["win"] += net
    else: x["nl"] += 1; x["loss"] += net
print(f"{'series':16s} {'n':>4s} {'won%':>5s} {'avg ask':>7s} {'stake':>6s} {'contr':>6s} {'avg win$':>8s} {'avg loss$':>9s} {'fee/bet':>7s} {'net':>7s} {'net/$1':>7s}")
for k, x in sorted(a.items()):
    print(f"{k:16s} {x['n']:4d} {100*x['nw']/x['n']:5.1f} {x['ask']/x['n']:7.3f} {x['stake']/x['n']:6.2f} {x['ctr']/x['n']:6.2f} {x['win']/max(x['nw'],1):8.3f} {x['loss']/max(x['nl'],1):9.3f} {x['fee']/x['n']:7.3f} {x['net']:+7.2f} {x['net']/x['stake']:+7.3f}")
print("DONE")
