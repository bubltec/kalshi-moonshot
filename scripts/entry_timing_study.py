"""Does the run-up before a buy decide whether it works? Every eligible moment of the finished windows, bucketed by how the ask moved over the 60 s before it. Places nothing.

Eligible: minute 5-12 of a 15-minute window, a side with its ask at 70-92c and a spread of 3c or less (what the crypto_favorite lane would buy). Bought at the ask, fee paid, held to the result.
Buckets: the ask now minus the ask 60 s ago (c), and how far the ask is under its own high of the last 90 s (c). Also the average ask change over the 60 s after, so a pop that fades shows up as a fall.
Standard errors across windows. Crypto, then all the 15-minute series.
    docker compose exec -T bot python - < scripts/entry_timing_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
OTHER = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M", "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")


def moments(series):
    out = []
    for m in main.db.execute(f"SELECT ticker, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", series).fetchall():
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
        if len(q) < 60:
            continue
        qts = [r[0] for r in q]
        close = m["close_ts"]
        for side in ("yes", "no"):
            asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
            bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
            win = 1.0 if m["result"] == side else 0.0
            t = close - 900 + 300
            while t < close - 900 + 720:
                i = bisect.bisect_right(qts, t) - 1
                t += 30
                if i < 0 or not (0.70 <= asks[i] <= 0.92) or asks[i] - bids[i] > 0.03:
                    continue
                b60, a60, b90 = bisect.bisect_right(qts, qts[i] - 60) - 1, bisect.bisect_right(qts, qts[i] + 60) - 1, bisect.bisect_right(qts, qts[i] - 90) - 1
                if b60 < 0 or b90 < 0:
                    continue
                c = asks[i] + fee(asks[i])
                out.append({"cl": m["ticker"], "run": (asks[i] - asks[b60]) * 100, "under": (max(asks[b90:i + 1]) - asks[i]) * 100, "after": (asks[a60] - asks[i]) * 100, "ret": (win - c) / c, "win": win})
    return out


def table(rows, key, edges, label):
    print(f"  {label}")
    for lo, hi in zip(edges, edges[1:], strict=False):
        g = [r for r in rows if lo <= r[key] < hi]
        if len(g) < 100:
            print(f"    {lo:+5.0f} to {hi:+5.0f}c  only {len(g)}")
            continue
        per = defaultdict(list)
        for r in g:
            per[r["cl"]].append(r["ret"])
        mm = [sum(v) / len(v) for v in per.values()]
        se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
        print(f"    {lo:+5.0f} to {hi:+5.0f}c  n={len(g):6} ({len(per):4} windows)  won {statistics.mean(r['win'] for r in g):4.0%}  ask 60 s later {statistics.mean(r['after'] for r in g):+5.1f}c  return {statistics.mean(r['ret'] for r in g):+6.1%} +-{se:.1f}")


for name, series in (("crypto", CRYPTO), ("all the other 15-minute series", OTHER)):
    rows = moments(series)
    print(f"\\n{name}: {len(rows):,} eligible moments, return {statistics.mean(r['ret'] for r in rows):+.1%}, ask 60 s later {statistics.mean(r['after'] for r in rows):+.1f}c on average")
    table(rows, "run", (-100, -6, -3, 0, 3, 6, 100), "by the ask's move over the 60 s before (c)")
    table(rows, "under", (0, 0.5, 2, 4, 8, 100), "by how far the ask is under its high of the last 90 s (c)")
