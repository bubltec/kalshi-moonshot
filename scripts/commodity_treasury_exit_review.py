"""Do the sales on the commodity lanes and treasury_favorite beat holding? (2026-11-09, user: "adjust the exit on some lanes (commodity, add it to treasury)").
Question: for every live bet on these lanes since 11-01, the sale (net of both fees) against holding the same contracts to the result. Per lane and exit kind: n, sale net, hold net, difference, and two halves of the period. Sales only; unsold bets reported as held. Not a rule: a review of what the exits did.
    docker compose exec -T bot python - < scripts/commodity_treasury_exit_review.py
"""
import collections, time
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
since = time.time() - 9 * 86400
rows = st.db.execute("""SELECT d.ts,d.lane,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_reason,w.result
  FROM decisions d JOIN windows w ON w.ticker=d.ticker
  WHERE d.action='bet' AND d.mode='trade' AND d.ts>? AND (d.lane LIKE 'commodity%' OR d.lane='treasury_favorite') AND w.result IN ('yes','no') ORDER BY d.ts""", (since,)).fetchall()
mid = rows[len(rows) // 2]["ts"] if rows else 0
agg = collections.defaultdict(lambda: [0, 0.0, 0.0, [0.0, 0.0]])
for r in rows:
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"] or 0)
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    if r["exit_price"]:
        er = (r["exit_reason"] or "other").split()[0].split(":")[0]
        sale = n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost
    else:
        er, sale = "held", hold
    for key in ((r["lane"], er), (r["lane"], "ALL")):
        a = agg[key]; a[0] += 1; a[1] += sale; a[2] += hold; a[3][0 if r["ts"] <= mid else 1] += sale - hold
print(f"{'lane':22s} {'exit':14s} {'n':>4s} {'sale net':>9s} {'hold net':>9s} {'diff':>8s}  halves diff")
for (lane, er), a in sorted(agg.items()):
    print(f"{lane:22s} {er:14s} {a[0]:4d} {a[1]:+9.2f} {a[2]:+9.2f} {a[1]-a[2]:+8.2f}  {a[3][0]:+.2f} / {a[3][1]:+.2f}")
print("DONE")
