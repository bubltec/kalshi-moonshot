"""The 10:45 ET crypto window of 2026-11-24 (user: "a real bad wave ... this should have exited at 34 at that peak, for easy early profit, then maybe picked up a NO buy later"): for each PriskP live bet in it, the price paid, the side's bid at its highest after the buy and when, the sale, and the result. Read-only; run on the host (scripts/cloud.sh run scripts/wave_1045_review.py)."""
import datetime as dt

from kalshi_moonshot import config, db

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
et = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=-5))).strftime("%H:%M:%S")  # noqa: E731
print(f"{'ticker':26s} {'paid':>5s} {'n':>5s} | {'peak bid':>8s} at | {'sold':>5s} at | result | net$ as ran | net$ if sold at the peak")
for r in st.execute("SELECT d.ticker, d.side, d.stake_usd, d.contracts, d.exit_price, d.exit_ts, d.ts, w.result, w.close_ts FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.lane='priskp' AND d.action='bet' AND d.stake_usd>0 AND d.ticker LIKE '%26NOV241045%' ORDER BY d.ts").fetchall():
    n = float(r["contracts"])
    paid = float(r["stake_usd"]) / n
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], float(r["ts"]), (float(r["close_ts"]) if r["close_ts"] else float(r["ts"]) + 900))).fetchall()
    sc = 100.0 if q and max(float(x[2]) for x in q) > 1.5 else 1.0
    sb = [(float(x[0]), (float(x[1]) / sc) if r["side"] == "yes" else 1.0 - float(x[2]) / sc) for x in q]
    pk = max(sb, key=lambda x: x[1])
    won = r["result"] == r["side"]; res = r["result"] or "?"
    fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731
    got = (float(r["exit_price"]) - fee(float(r["exit_price"]))) if r["exit_price"] else (1.0 if won else 0.0)
    asran = (got - paid - fee(paid)) * n
    atpk = (pk[1] - fee(pk[1]) - paid - fee(paid)) * n
    print(f"{r['ticker'][:22]:26s} {paid:5.2f} {n:5.1f} | {pk[1]:8.2f} {et(pk[0])} | {'-' if not r['exit_price'] else format(float(r['exit_price']), '5.2f'):>5s} {et(float(r['exit_ts'])) if r['exit_ts'] else '-':>8s} | {res:>3s}    | {asran:+8.2f}    | {atpk:+8.2f}")
