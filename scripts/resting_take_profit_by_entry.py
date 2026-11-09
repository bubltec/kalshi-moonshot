"""(second cut, user: "on these super early buys why not take the 98%") Same bets and sale as below, split by how early the buy was (minutes into the window at the buy: under 4, 4 to 8, 8 or later) and, for the touch, by the seconds left when it happened. P in 0.95 and 0.98 only.
A resting take-profit sell placed when the buy fills (2026-11-09, user: "if you placed the sell order at 93 or 98 even ... we do not pay a fee, because the sell order is placed when we bought in").
Question, on the same 1398 live crypto and commodity 15-minute bets since 11-05: a post-only sell at P rests from the buy to the close. It fills at P, with no fee, the first time the held side's bid is at or over P at least 10 s before the close (the best case: a touch fills; no queue). Compare with what the lane actually did (its own sale or the result); a bet the lane sold earlier is not counted. P in 0.90, 0.93, 0.95, 0.98 (chosen before the results). Per family: bets, touched, then lost at the close, change in dollars against what happened, sale beat/lost, two halves.
Not covered: the fill itself (queue position, a touch that does not trade through), the re-buy.
    docker compose exec -T bot python - < scripts/resting_take_profit.py
"""
import os
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(15)
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.db.execute("""SELECT d.ts,d.lane,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_ts,w.result,w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker
  WHERE d.action='bet' AND d.mode='trade' AND d.ts>? AND w.result IN ('yes','no') AND d.contracts>0 AND d.ticker LIKE '%15M%' AND (d.lane LIKE 'crypto%' OR d.lane LIKE 'commodity%') AND d.lane<>'commodity' ORDER BY d.ts""", (1_793_700_000,)).fetchall()
mid = rows[len(rows) // 2]["ts"]
PS = (0.95, 0.98)
BUCKETS = (('buy under 4 min in', 0, 240), ('buy 4-8 min in', 240, 480), ('buy 8+ min in', 480, 900))
res = {(f, b[0], p): dict(n=0, hit=0, lost=0, diff=0.0, beat=0, lose=0, half=[0.0, 0.0], hn=[0, 0], left=0.0) for f in ("crypto", "commodity") for b in BUCKETS for p in PS}
for r in rows:
    fam = "crypto" if r["lane"].startswith("crypto") else "commodity"
    n, cost, close = float(r["contracts"]), float(r["stake_usd"]), float(r["close_ts"])
    actual = n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost if r["exit_price"] else (n if r["result"] == r["side"] else 0.0) - cost
    t_exit = float(r["exit_ts"]) if r["exit_ts"] else None
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], float(r["ts"]), close - 10)).fetchall()
    held = [(float(x[0]), float(x[1]) if r["side"] == "yes" else 1.0 - float(x[2])) for x in q]
    el = float(r["ts"]) - (close - 900.0)
    bk = next(b[0] for b in BUCKETS if b[1] <= el < b[2]) if 0 <= el < 900 else None
    if bk is None:
        continue
    for p in PS:
        x = res[(fam, bk, p)]
        x["n"] += 1
        got = next(((t, b) for t, b in held if b >= p - 1e-9 and (t_exit is None or t < t_exit)), None)
        if got is None:
            continue
        x["hit"] += 1
        x["left"] += close - got[0]
        if r["result"] != r["side"]: x["lost"] += 1
        d = (n * p - cost) - actual
        x["diff"] += d; x["beat" if d > 0 else "lose"] += 1
        k = 0 if r["ts"] <= mid else 1
        x["half"][k] += d; x["hn"][k] += 1
print(f"{'family':9s} {'bucket':19s} {'sell at':>7s} {'bets':>5s} {'touched':>7s} {'avg left':>8s} {'then lost':>9s} {'diff $':>8s} {'beat/lost':>11s}  halves diff (n)")
for (fam, bk, p), x in sorted(res.items()):
    print(f"{fam:9s} {bk:19s} {p:7.2f} {x['n']:5d} {x['hit']:7d} {x['left'] / max(x['hit'], 1):7.0f}s {x['lost']:9d} {x['diff']:+8.2f} {x['beat']:5d}/{x['lose']:<5d}  {x['half'][0]:+.2f} ({x['hn'][0]}) / {x['half'][1]:+.2f} ({x['hn'][1]})")
print("DONE")
