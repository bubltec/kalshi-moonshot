"""GUARD OVER THE WHOLE WINDOW: replay the live settlement guard (crypto_favorite rules: buy a side at 0.70-0.92 with 5-12 minutes left, then sell when the exact chance of finishing ahead falls under the bid by `delta`) from entry to 20 s before the close, using settlement.chance_above, and compare cashing out with holding to the result, by how many seconds were left when it fired. Per contract held, taker fee on the buy and the sale. Places nothing.
    docker compose exec -T bot python - < scripts/guard_fullwindow_study.py
"""
import bisect
import math
import statistics

from kalshi_moonshot import config, db, settlement
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
STEP = 3
paths = []  # per window: dict(side, hold, entry_ask, pts=[(sec, p_hold, bid_hold)])
for series in SERIES:
    ix = index_for_series(series)
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    its, ivs = [x[0] for x in r], [x[1] for x in r]
    for w in main.db.execute("SELECT ticker, result, close_ts, strike FROM windows WHERE series=? AND result IN ('yes','no') AND strike IS NOT NULL", (series,)).fetchall():
        T, K = w["close_ts"], float(w["strike"])
        if T - 3600 < its[0]:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"], T - 900, T)).fetchall()
        if len(q) < 50:
            continue
        qts = [x[0] for x in q]
        lo = bisect.bisect_left(its, T - 1900)
        hi = bisect.bisect_right(its, T)
        ts_, vs_ = its[lo:hi], ivs[lo:hi]
        side = None
        pts = []
        for sec in range(720, 19, -STEP):
            t = T - sec
            i = bisect.bisect_right(qts, t) - 1
            if i < 0 or t - qts[i] > 30:
                continue
            bid, ask = q[i][1], q[i][2]
            if side is None:
                if sec > 720 or sec < 300:
                    continue
                if 0.70 <= ask <= 0.92:
                    side, entry = "yes", ask
                elif 0.70 <= 1 - bid <= 0.92:
                    side, entry = "no", 1 - bid
                else:
                    continue
            j = bisect.bisect_right(ts_, t)
            sig = settlement.sigma_per_second(ts_[:j], vs_[:j], t)
            if sig is None or j == 0 or t - ts_[j - 1] > 5:
                continue
            spot = vs_[j - 1]
            locked = 0.0
            if sec < 60:
                locked = settlement.locked_average(ts_[:j], vs_[:j], T, t)
                if locked is None:
                    continue
            p = settlement.chance_above(K, spot, locked, float(sec), sig)
            if p is None:
                continue
            p_hold = p if side == "yes" else 1 - p
            bid_hold = bid if side == "yes" else 1 - ask
            pts.append((sec, p_hold, bid_hold))
        if side:
            hold = (1.0 if w["result"] == "yes" else 0.0) if side == "yes" else (0.0 if w["result"] == "yes" else 1.0)
            paths.append({"hold": hold, "entry": entry, "pts": pts})
print(len(paths), "windows with an entry")
for delta in (0.10, 0.15, 0.20, 0.30):
    fired = []
    for pth in paths:
        for sec, ph, bh in pth["pts"]:
            if ph < bh - delta:
                fired.append((sec, bh - fee(bh) - pth["hold"], pth["hold"]))
                break
    print(f"delta {delta:.2f}: fired on {len(fired)} of {len(paths)} windows ({100 * len(fired) / len(paths):.0f}%)")
    for a, b in ((420, 721), (240, 420), (150, 240), (60, 150), (20, 60)):
        g = [f for f in fired if a <= f[0] < b]
        if len(g) >= 5:
            d = [f[1] for f in g]
            print(f"   {a:3}-{b - 1:3} s left: {len(g):3} fired, the position then won {100 * statistics.mean(f[2] for f in g):3.0f}%; cash out minus hold {100 * statistics.mean(d):+6.2f}c +-{100 * statistics.pstdev(d) / math.sqrt(len(d)):.2f}")
