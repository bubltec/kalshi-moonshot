"""Does selling a bet the moment its bid reaches a high price, with minutes left, beat what the lane did? (2026-11-09, user: ETH and XRP DOWN bought at 0.66-0.68 reached 0.93 with 10 minutes left: "we should be cashing out ... buy again for the uptick".)
Question, on the same live bets (crypto and commodity 15-minute lanes, trade mode, since 11-05): for each bet, the first moment after the buy with the held side's bid at or over H and at least L seconds left; the sale there nets contracts x (bid - fee(bid)) - cost; compare with what the lane actually did (its own sale, or held to the result). A bet the lane had already sold before that moment is not counted. Grid H in 0.90, 0.93, 0.95; L in 300, 420, 600 (chosen before the results, not tuned). Reported per family: bets that touched the line, how many of those then lost at the close, the change in dollars against what happened, how many the sale beat and lost to, and the two halves of the period.
Not covered: the re-buy after the sale (the opposite side or the same side on the dip), queue and fill at the bid, resting (fee-free) sales, curve shapes.
    docker compose exec -T bot python - < scripts/early_profit_sale.py
"""
import bisect, collections, os, time
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(15)
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
since = 1_793_700_000  # 2026-11-05 ~ 00:00 ET
rows = st.db.execute("""SELECT d.ts,d.lane,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_ts,w.result,w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker
  WHERE d.action='bet' AND d.mode='trade' AND d.ts>? AND w.result IN ('yes','no') AND d.contracts>0 AND d.ticker LIKE '%15M%' AND (d.lane LIKE 'crypto%' OR d.lane LIKE 'commodity%') AND d.lane<>'commodity' ORDER BY d.ts""", (since,)).fetchall()
print("bets", len(rows))
mid = rows[len(rows) // 2]["ts"]
GRID = [(h, l) for h in (0.90, 0.93, 0.95) for l in (300, 420, 600)]
res = {(fam, g): {"n": 0, "touched": 0, "lost_after": 0, "diff": 0.0, "beat": 0, "lose": 0, "half": [0.0, 0.0], "halfn": [0, 0]} for fam in ("crypto", "commodity") for g in GRID}
for r in rows:
    fam = "crypto" if r["lane"].startswith("crypto") else "commodity"
    n, cost, close = float(r["contracts"]), float(r["stake_usd"]), float(r["close_ts"])
    actual = n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost if r["exit_price"] else (n if r["result"] == r["side"] else 0.0) - cost
    t_exit = float(r["exit_ts"]) if r["exit_ts"] else None
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], float(r["ts"]), close)).fetchall()
    held = [(float(x[0]), float(x[1]) if r["side"] == "yes" else 1.0 - float(x[2])) for x in q]
    for g in GRID:
        h, l = g
        x = res[(fam, g)]
        x["n"] += 1
        hit = next(((t, b) for t, b in held if b >= h and close - t >= l and (t_exit is None or t < t_exit)), None)
        if hit is None:
            continue
        x["touched"] += 1
        sale = n * (hit[1] - fee(hit[1])) - cost
        won = r["result"] == r["side"]
        if not won:
            x["lost_after"] += 1
        d = sale - actual
        x["diff"] += d
        x["beat" if d > 0 else "lose"] += 1
        k = 0 if r["ts"] <= mid else 1
        x["half"][k] += d; x["halfn"][k] += 1
print(f"{'family':9s} {'bid>=':>5s} {'left>=':>6s} {'bets':>5s} {'touched':>7s} {'then lost':>9s} {'diff $':>8s} {'sale beat/lost':>14s}  halves diff (n)")
for (fam, g), x in sorted(res.items()):
    print(f"{fam:9s} {g[0]:5.2f} {g[1]:6d} {x['n']:5d} {x['touched']:7d} {x['lost_after']:9d} {x['diff']:+8.2f} {x['beat']:6d}/{x['lose']:<6d}  {x['half'][0]:+.2f} ({x['halfn'][0]}) / {x['half'][1]:+.2f} ({x['halfn'][1]})")
print("DONE")
