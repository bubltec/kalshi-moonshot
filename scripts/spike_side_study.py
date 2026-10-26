"""Whichever side spikes first is the tell? Both sides watched, every finished 15-minute crypto market, whether or not anything is held. Replayed from the tape. Places nothing.

Per second from minute 3 to 20 seconds before the close: the contracts traded by YES takers and by NO takers in the last 3 seconds, each against the market's own normal side-volume (half the median per-second volume of the 2 minutes before, times 3).
A spike is a side's 3-second volume at or over R times that, and at least 100 contracts. The first second either side spikes marks the market: the spiking side is the aggressor (if both spike in the same second, the bigger).
Then: FOLLOW buys the aggressor's side at its ask at that second and holds to the result, FADE buys the other side; both fee paid and fee-free, and the aggressor side's mid move over the next 30 seconds.
Per coin; standard errors across markets.
    docker compose exec -T bot python - < scripts/spike_side_study.py
"""
import bisect
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
RATIOS = (3, 5, 10, 20)
results = {sr: {r: [] for r in RATIOS} for sr in SERIES}
for sr in SERIES:
    for m in main.db.execute("SELECT ticker, result, close_ts FROM windows WHERE series=? AND result IN ('yes','no') ORDER BY close_ts DESC LIMIT 600", (sr,)).fetchall():
        close = m["close_ts"]
        fl = tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts>? AND ts<=?", (m["ticker"], int(close - 900), int(close))).fetchall()
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 900, close)).fetchall()
        if len(fl) < 60 or len(q) < 30:
            continue
        qts = [x[0] for x in q]
        fby = {int(x[0]): (x[1], x[2]) for x in fl}
        first = {r: None for r in RATIOS}
        for sec in range(720, 19, -1):  # from minute 3 (720 s before the close)
            t = int(close - sec)
            y3 = sum(fby.get(t - k, (0, 0))[0] for k in range(3))
            n3 = sum(fby.get(t - k, (0, 0))[1] for k in range(3))
            per = sorted(sum(fby.get(t - 3 - k, (0, 0))) for k in range(120))
            base = (per[59] + per[60]) / 2.0
            norm = 3.0 * base / 2.0 + 1.0  # a side's normal 3 seconds
            for r in RATIOS:
                if first[r] is not None:
                    continue
                ry, rn = y3 / norm, n3 / norm
                if max(ry, rn) >= r and max(y3, n3) >= 100:
                    side = "yes" if ry >= rn else "no"
                    i = bisect.bisect_right(qts, t) - 1
                    if i < 0 or t - qts[i] > 30:
                        continue
                    j = bisect.bisect_right(qts, t + 30) - 1
                    bid, ask = q[i][1], q[i][2]
                    price = ask if side == "yes" else 1 - bid
                    mid0 = (bid + ask) / 2
                    mid1 = (q[j][1] + q[j][2]) / 2
                    move = (mid1 - mid0) if side == "yes" else -(mid1 - mid0)
                    won = 1.0 if m["result"] == side else 0.0
                    first[r] = (price, won, move, 1 - price if 0 < price < 1 else None)
        for r in RATIOS:
            if first[r] is not None:
                results[sr][r].append((m["ticker"], *first[r]))
print("coin  spike | markets with a spike (of those scanned) | aggressor side wins | price paid | FOLLOW return with fee / fee-free | FADE return with fee | aggressor side's mid over the next 30 s")
for sr in SERIES:
    for r in RATIOS:
        g = results[sr][r]
        if len(g) < 40:
            print(f"{sr[2:5]:4} {r:3}x | only {len(g)}")
            continue
        fol = [((w - p - fee(p)) / (p + fee(p))) for _, p, w, _, _ in g]
        folg = [((w - p) / p) for _, p, w, _, _ in g]
        po = [1 - p for _, p, w, _, _ in g]
        fad = [(((1 - w) - q - fee(q)) / (q + fee(q))) for q, (_, p, w, _, _) in zip(po, g, strict=True) if 0.01 < q < 0.99]
        print(f"{sr[2:5]:4} {r:3}x | {len(g):4} | {100 * statistics.mean(w for _, _, w, _, _ in g):4.1f}% | {100 * statistics.mean(p for _, p, *_ in g):4.1f}c | {100 * statistics.mean(fol):+6.1f}% +-{100 * statistics.pstdev(fol) / math.sqrt(len(fol)):.0f} / {100 * statistics.mean(folg):+6.1f}% | {100 * statistics.mean(fad):+6.1f}% | {100 * statistics.mean(mv for _, _, _, mv, _ in g):+5.2f}c")
