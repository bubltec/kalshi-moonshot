"""Would a resting sell save money on the value exit? (2026-11-08, user: "do 3 and 5 from your list"; item 5: maker fills. Every exit is a taker order that crosses the book and pays the taker fee; a resting post-only sell pays none and sells at the ask instead of the bid.)
Question: for each live value-exit sale since 11-05 on crypto_multiscale, compare what the sale actually netted (the exit price less the sell fee) with this alternative: at the trigger second rest a post-only sell at the position's ask price (for a YES position the YES ask, for a NO position 1 - the YES bid); it fills with no fee if the side's bid reaches that price within W seconds (a buyer meets it; read from the tape's quotes, conservative: the bid must reach the ask, a trade at the ask without a bid move is not counted); if it has not filled after W seconds, take the bid then (taker, full fee). W in 5, 10, 20 s is a grid fixed before the results.
Reported per W: sales, how many filled as a maker, the net change per sale against what the live sale made (cents per contract and dollars in total) with its standard error, and the cost of waiting: the sales that fell back to the taker at a lower bid. Also the same for the shock exit's sales (waiting is riskier there: the bid is falling).
Not covered: queue position (a resting sell at the ask joins the back of the queue and may not fill even when the bid reaches it: the replay is generous), the position of the settlement window, partial fills.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/maker_exit_replay.py
"""

import bisect
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
WS = (5, 10, 20)


def quotes(ticker, t0, t1):
    r = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, t0 - 5, t1 + 2)).fetchall()
    return [float(x[0]) for x in r], [(float(x[1]), float(x[2])) for x in r]


def side_prices(side, yb, ya):
    return (yb, ya) if side == "yes" else (1.0 - ya, 1.0 - yb)  # (bid, ask) of the side held


def run(kind, label):
    rows = st.db.execute(
        "SELECT ticker,side,contracts,exit_ts,exit_price FROM decisions WHERE action='bet' AND mode='trade' AND lane='crypto_multiscale' AND exit_reason LIKE ? AND exit_price>0 AND contracts>0 AND exit_ts>? ORDER BY exit_ts",
        (kind + "%", 1794000000),
    ).fetchall()
    print(f"\n{label}: {len(rows)} live sales since 11-05")
    for W in WS:
        diffs, maker, fallback_loss = [], 0, 0
        for r in rows:
            t = float(r["exit_ts"])
            ts, qs = quotes(r["ticker"], t, t + W)
            i = bisect.bisect_right(ts, t) - 1
            if i < 0:
                continue
            bid0, ask0 = side_prices(r["side"], *qs[i])
            n = float(r["contracts"])
            live = n * (float(r["exit_price"]) - fee(float(r["exit_price"])))
            filled = None
            for j in range(i + 1, len(ts)):
                if ts[j] > t + W:
                    break
                b, _a = side_prices(r["side"], *qs[j])
                if b >= ask0 - 1e-9:
                    filled = n * ask0
                    break
            if filled is not None:
                maker += 1
                alt = filled
            else:
                k = bisect.bisect_right(ts, t + W) - 1
                b, _a = side_prices(r["side"], *qs[max(k, i)])
                alt = n * (b - fee(b))
                if alt < live:
                    fallback_loss += 1
            diffs.append(alt - live)
        m = sum(diffs) / max(len(diffs), 1)
        se = (sum((x - m) ** 2 for x in diffs) / max(len(diffs) - 1, 1)) ** 0.5 / math.sqrt(max(len(diffs), 1))
        print(f"   W={W:2d} s: sales {len(diffs)}, filled as a maker {maker}, fell back to the taker {len(diffs) - maker} ({fallback_loss} of them for less than the live sale) | change per sale {m:+.3f} +/- {se:.3f} dollars, total {sum(diffs):+.2f}")


run("value exit", "the value exit")
run("shock exit", "the shock exit")
print("DONE")
