"""The 12:30 ET window of 2026-11-24 (user: "this DOGE market could have exited around mid market when it showed a high profit point. since it was a slow curve, we should have exited"; "i just don't get why we didn't cash out for profit"): replay the early_take step's rule as built (arm when the bid after fees is over the price paid, follow the high, sell when the bid falls from it by the bid's own typical 10 s move or has no new high for a tenth of the time left) on each PriskP bet of that window, with the step on the first half of the market (as live) and on the whole market, against what really happened. Quotes from the tape, a sale at the recorded bid, no fill modelled. Read-only; run on the host."""
import bisect
import math

from kalshi_moonshot import config, db, quoteshock

import os

BAR = os.environ.get("BAR", "1") == "1"  # 2026-11-24: arm only once the profit is one wobble (the bid's typical 10 s move); BAR=0 = any profit
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.execute("SELECT d.lane, d.ticker, d.side, d.stake_usd, d.contracts, d.exit_price, d.ts, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.stake_usd>0 AND d.ticker LIKE '%26NOV241230-30' AND d.lane IN ('priskp','manual') ORDER BY d.ts").fetchall()
print(f"{'bet':22s} {'paid':>5s} {'n':>5s} | {'actual':>7s} | {'half':>14s} | {'whole market':>14s}   (sale price @ time ET, net $)")
for r in rows:
    n, paid = float(r["contracts"]), float(r["stake_usd"]) / float(r["contracts"])
    close, t0 = float(r["close_ts"]), float(r["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], close - 1500.0, close)).fetchall()
    sc = 100.0 if max(float(x[2]) for x in q) > 1.5 else 1.0
    ts = [float(x[0]) for x in q]
    yb, ya = [float(x[1]) / sc for x in q], [float(x[2]) / sc for x in q]
    sb = yb if r["side"] == "yes" else [1.0 - a for a in ya]
    mids = [(ts[i], (yb[i] + ya[i]) / 2.0 if r["side"] == "yes" else 1.0 - (yb[i] + ya[i]) / 2.0) for i in range(len(ts))]
    won = r["result"] == r["side"]
    cost = paid + fee(paid)
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"]))) if r["exit_price"] else (1.0 if won else 0.0)) - cost

    def run(share):
        armed, high, high_ts = False, 0.0, 0.0
        for sec in range(int(t0) + 1, int(close) - 1):
            j = bisect.bisect_right(ts, sec) - 1
            if j < 0 or sec - ts[j] > 30:
                continue
            bid, left = sb[j], close - sec
            if left <= (1.0 - share) * 900.0:
                return None
            net = bid - fee(bid) - cost
            unit = quoteshock.typical_minute([m for m in mids if m[0] <= sec], sec)
            trail = max(0.01, unit * math.sqrt(10.0 / 60.0)) if unit else 0.01
            if not armed:
                if net < (trail if BAR else 0.0) or net <= 0:
                    continue
                armed, high, high_ts = True, bid, sec
            if bid > high + 1e-9:
                high, high_ts = bid, sec
            if net <= 0:
                continue
            if high - bid >= trail - 1e-9 or sec - high_ts >= 0.1 * left:
                return bid, sec, net
        return None

    def show(x):
        if x is None:
            return f"{'no sale':>14s}"
        bid, sec, net = x
        hh = (sec - 5 * 3600) % 86400
        return f"{bid:.2f}@{hh // 3600:02d}:{hh % 3600 // 60:02d}:{hh % 60:02d} {net * n:+.1f}"

    print(f"{r['lane'][:6]:6s} {r['ticker'][2:11]:15s} {paid:5.2f} {n:5.1f} | {actual * n:+7.2f} | {show(run(0.5)):>14s} | {show(run(1.0)):>14s}   result {r['result']}")
