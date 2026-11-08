"""The live value exit against holding (2026-11-08, user: "it is working but it is not reaping what I expected").

Question: the value exit sales since 07:00 ET today: what did each net after both fees (the position's cost in stake_usd against the sale at its price less the sell fee), and what would the same bets have returned held to the result? Not covered: the shock exit and the other exits that would have fired on the held bets, bets still open, sales that slipped under their trigger bid.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/value_exit_live_review.py
"""

import datetime

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload(); st = Store(s.db_path, s.exclude_versions)
ET = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone(datetime.timedelta(hours=-5))).strftime("%H:%M:%S")
rows = st.db.execute("SELECT d.ts,d.ticker,d.lane,d.side,d.ask,d.contracts,d.stake_usd,d.exit_ts,d.exit_price,d.exit_reason,w.result FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.exit_reason LIKE 'value exit%' AND d.exit_ts>? ORDER BY d.exit_ts", (1794100000,)).fetchall()
print("value exit sales since 11-08 07:00 ET:", len(rows))
tot_sale = tot_cost = tot_hold = 0.0; settled = 0
for r in rows:
    n = float(r["contracts"]); cost = float(r["stake_usd"] or 0); sale = n * (float(r["exit_price"]) - fee(float(r["exit_price"])))
    res = r["result"]; hold = (n if res == r["side"] else 0.0) if res in ("yes", "no") else None
    tot_sale += sale; tot_cost += cost
    if hold is not None: tot_hold += hold; settled += 1
    held = "won" if hold and hold > 0 else ("lost" if hold == 0 else "open")
    print(f"  {ET(r['ts'])}->{ET(r['exit_ts'])} {r['ticker'][:22]:22s} {r['side']} paid~{cost/n:.2f} sold {r['exit_price']:.2f} net {sale-cost:+.2f} | held would have: {held} ({(hold-cost) if hold is not None else float('nan'):+.2f})")
print(f"sold: ${tot_sale:.2f} vs cost ${tot_cost:.2f} = {tot_sale-tot_cost:+.2f}; the same bets held (settled {settled}): {tot_hold - sum(float(r['stake_usd'] or 0) for r in rows if r['result'] in ('yes','no')):+.2f}")
print("DONE")
