"""Is the bot's buy timing bad? Each real crypto_favorite buy against the other moments of the same market and side. Read-only.

For every real buy (a settled crypto_favorite bet in trade mode) the tape gives the side's ask at every quote of that window. The eligible moments are those the lane would have bought: minute 5-12 of the window, ask 70-92c, spread 3c or less.
Reported: where the price the bot paid sits among the eligible prices of its own market (0% = the cheapest moment, 100% = the dearest), how many cents it paid above that market's average and cheapest eligible price,
whether the price at the buy was a local peak (how far the ask moved over the 60 s before and after), and what waiting would have done. The result itself is the same for every moment of one market, so a difference is all timing.
    docker compose exec -T bot python - < scripts/buy_timing_study.py
"""
import bisect
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
store = Store(s.db_path, s.exclude_versions)
rows = store.db.execute("SELECT ts, ticker, side, ask, outcome, close_time FROM decisions WHERE lane='crypto_favorite' AND mode='trade' AND action='bet' AND outcome IN ('yes','no') AND ts>? ORDER BY ts", (time.time() - 14 * 86400,)).fetchall()
res = []
for r in rows:
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
    if len(q) < 60:
        continue
    qts = [x[0] for x in q]
    asks = [x[2] if r["side"] == "yes" else 1 - x[1] for x in q]
    bids = [x[1] if r["side"] == "yes" else 1 - x[2] for x in q]
    close = max(qts[-1], r["ts"])
    from datetime import datetime

    close = datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    elig = [asks[k] for k in range(len(q)) if close - 900 + 300 <= qts[k] <= close - 900 + 720 and 0.70 <= asks[k] <= 0.92 and asks[k] - bids[k] <= 0.03]
    if len(elig) < 8:
        continue
    i = bisect.bisect_right(qts, r["ts"]) - 1
    if i < 0:
        continue
    paid = float(r["ask"])
    before, after1, after2 = bisect.bisect_right(qts, r["ts"] - 60) - 1, bisect.bisect_right(qts, r["ts"] + 60) - 1, bisect.bisect_right(qts, r["ts"] + 120) - 1
    res.append({"pct": sum(1 for e in elig if e < paid) / len(elig), "vs_mean": paid - statistics.mean(elig), "vs_min": paid - min(elig), "won": r["outcome"] == r["side"],
                "run_before": paid - asks[before] if before >= 0 else None, "move_after_60": asks[after1] - paid, "move_after_120": asks[after2] - paid, "n": len(elig)})
print(f"{len(res)} real buys with a tape (of {len(rows)} settled)")
if res:
    print(f"price paid sits at the {100 * statistics.mean(x['pct'] for x in res):.0f}th percentile of its market's eligible prices on average (50 = no better than a random moment)")
    print(f"paid {100 * statistics.mean(x['vs_mean'] for x in res):+.1f}c vs that market's average eligible price, {100 * statistics.mean(x['vs_min'] for x in res):+.1f}c vs its cheapest")
    print(f"the ask had risen {100 * statistics.mean(x['run_before'] for x in res if x['run_before'] is not None):+.1f}c over the 60 s before the buy;  after it: {100 * statistics.mean(x['move_after_60'] for x in res):+.1f}c over 60 s, {100 * statistics.mean(x['move_after_120'] for x in res):+.1f}c over 120 s")
    for label, grp in (("won", [x for x in res if x["won"]]), ("lost", [x for x in res if not x["won"]])):
        if grp:
            print(f"  {label:5} n={len(grp):3}  percentile {100 * statistics.mean(x['pct'] for x in grp):3.0f}  vs average {100 * statistics.mean(x['vs_mean'] for x in grp):+5.1f}c  ask 60 s later {100 * statistics.mean(x['move_after_60'] for x in grp):+5.1f}c")
