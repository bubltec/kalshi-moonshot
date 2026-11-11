"""The big commodity losses of the last 14 hours, bet by bet (2026-11-09, user: "50% losses are hard at $10 bets").
Question: for every commodity-lane bet since SINCE that lost 25% or more of its stake, when was it bought (minutes in, price), how did it end, and what did the shock log and the bid path show. Review only; no rule.
    docker compose exec -T bot python - < scripts/commodity_loss_review.py
"""
import datetime, os, time
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone(datetime.timedelta(hours=-5))).strftime("%m-%d %H:%M:%S")
since = time.time() - float(os.environ.get("HOURS", "14")) * 3600
rows = st.db.execute("""SELECT d.ts,d.lane,d.ticker,d.side,d.ask,d.contracts,d.stake_usd,d.exit_price,d.exit_reason,d.close_time,w.result,w.close_ts
  FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.ts>? AND d.lane LIKE 'commodity%' ORDER BY d.ts""", (since,)).fetchall()
bad = []
for r in rows:
    n, c = float(r["contracts"] or 0), float(r["stake_usd"] or 0)
    if r["exit_price"]: net = n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - c
    elif r["result"] in ("yes", "no"): net = (n if r["result"] == r["side"] else 0.0) - c
    else: continue
    if c > 0 and net / c <= -0.25: bad.append((r, net, c))
print("commodity bets", len(rows), "losing 25%+:", len(bad), "total", round(sum(b[1] for b in bad), 2))
for r, net, c in bad:
    close = float(r["close_ts"]) if r["close_ts"] else None
    inm = (15 - (close - float(r["ts"])) / 60) if close else None
    print(f"\n{ET(r['ts'])} {r['lane']} {r['ticker']} {r['side']} ask {float(r['ask']):.2f} stake ${c:.2f} in at min {inm:.1f}" if inm is not None else f"\n{ET(r['ts'])} {r['lane']} {r['ticker']}")
    print(f"   ended: {(r['exit_reason'] or 'held to ' + str(r['result']))[:150]}  net {net:+.2f} ({100 * net / c:+.0f}%)")
    for sl in st.db.execute("SELECT ts,left_s,bid,cushion_bp,move_bp,worst_bp,fired FROM shock_log WHERE ticker=? AND lane=? ORDER BY ts LIMIT 6", (r["ticker"], r["lane"])).fetchall():
        print(f"   shock_log {ET(sl['ts'])} left {sl['left_s']:.0f}s bid {sl['bid']:.2f} cushion {sl['cushion_bp']:+.1f} move {sl['move_bp']:+.1f} worst {sl['worst_bp']:+.1f} fired {sl['fired']}")
    if close:
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], float(r["ts"]) - 1, close)).fetchall()
        step = max(1, len(q) // 14)
        path = [(float(x[0]), float(x[1]), float(x[2])) for x in q[::step]]
        held = lambda b, a: b if r["side"] == "yes" else 1 - a
        print("   held bid path:", " ".join(f"{(p[0] - float(r['ts'])) / 60:.0f}m:{held(p[1], p[2]):.2f}" for p in path))
print("DONE")
