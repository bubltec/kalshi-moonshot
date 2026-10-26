"""The state of the move at the moment of the buy (the last 60 s against the last 15 s). Original doc: Does the run-up before a buy decide whether it works? Every eligible moment of the finished windows, bucketed by how the ask moved over the 60 s before it. Places nothing.

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
                t += 15
                if i < 0 or not (0.70 <= asks[i] <= 0.92) or asks[i] - bids[i] > 0.03:
                    continue
                b60, b15, a60 = bisect.bisect_right(qts, qts[i] - 60) - 1, bisect.bisect_right(qts, qts[i] - 15) - 1, bisect.bisect_right(qts, qts[i] + 60) - 1
                if b60 < 0 or b15 < 0:
                    continue
                m60, m15 = (asks[i] - asks[b60]) * 100, (asks[i] - asks[b15]) * 100
                big = 3.0
                if m60 >= big:
                    state = "rose, still rising" if m15 >= 1.0 else "rose, then pulled back" if m15 <= -1.0 else "rose, then settled flat"
                elif m60 <= -big:
                    state = "fell, still falling" if m15 <= -1.0 else "fell, now bouncing" if m15 >= 1.0 else "fell, then settled flat"
                else:
                    state = "no big move"
                c = asks[i] + fee(asks[i])
                out.append({"cl": m["ticker"], "state": state, "ret": (win - c) / c, "gross": (win - asks[i]) / asks[i], "win": win, "after": (asks[a60] - asks[i]) * 100})
    return out


ORDER = ("rose, still rising", "rose, then settled flat", "rose, then pulled back", "fell, still falling", "fell, then settled flat", "fell, now bouncing", "no big move")
for name, series in (("crypto", CRYPTO), ("all the other 15-minute series", OTHER)):
    rows = moments(series)
    print(f"\n{name}: {len(rows):,} eligible moments (every 15 s, minute 5-12), return with the fee {statistics.mean(r['ret'] for r in rows):+.1%}, without {statistics.mean(r['gross'] for r in rows):+.1%}")
    for st in ORDER:
        g = [r for r in rows if r["state"] == st]
        if len(g) < 100:
            print(f"  {st:26} only {len(g)}")
            continue
        per = defaultdict(list)
        for r in g:
            per[r["cl"]].append(r["ret"])
        mm = [sum(v) / len(v) for v in per.values()]
        se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
        print(f"  {st:26} n={len(g):6} ({len(per):4} windows)  won {statistics.mean(r['win'] for r in g):4.0%}  ask 60 s later {statistics.mean(r['after'] for r in g):+5.1f}c  return with the fee {statistics.mean(r['ret'] for r in g):+6.1%}, without {statistics.mean(r['gross'] for r in g):+6.1%} +-{se:.1f}")
