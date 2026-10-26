"""Per coin, and the last 3 hours against the rest. Original: The state of the move at the moment of the buy (the last 60 s against the last 15 s). Original doc: Does the run-up before a buy decide whether it works? Every eligible moment of the finished windows, bucketed by how the ask moved over the 60 s before it. Places nothing.

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
                out.append({"ask": asks[i], "cl": m["ticker"], "coin": m["ticker"].split("-")[0], "t": close, "state": state, "ret": (win - c) / c, "gross": (win - asks[i]) / asks[i], "win": win, "after": (asks[a60] - asks[i]) * 100})
    return out


import time
rows = moments(CRYPTO)
now = max(r["t"] for r in rows)
recent = now - 3 * 3600


def stats(g):
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["gross"])
    if len(per) < 5:
        return "too few"
    mm = [sum(v) / len(v) for v in per.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
    return f"n={len(g):5} ({len(per):4} win) won {statistics.mean(r['win'] for r in g):4.0%} ask {statistics.mean(r['ask'] for r in g):.2f} fee-free return {statistics.mean(r['gross'] for r in g):+6.1%} +-{se:.1f}"


print(f"{len(rows):,} eligible crypto moments; the last 3 hours hold {sum(1 for r in rows if r['t'] > recent):,}")
print("\nper coin, whole tape / last 3 hours")
for coin in sorted({r["coin"] for r in rows}):
    g = [r for r in rows if r["coin"] == coin]
    print(f"  {coin:9} all    {stats(g)}")
    print(f"  {'':9} 3h     {stats([r for r in g if r['t'] > recent])}")
print("\nper coin by state (fee-free return, whole tape)")
for coin in sorted({r["coin"] for r in rows}):
    g = [r for r in rows if r["coin"] == coin]
    cells = []
    for st in ("rose, still rising", "rose, then pulled back", "fell, still falling", "no big move"):
        x = [r for r in g if r["state"] == st]
        cells.append(f"{st.split(',')[0]}/{st.split(', ')[1][:7]} {statistics.mean(r['gross'] for r in x):+5.1%} (n={len(x)})" if len(x) > 80 else f"{st[:12]} few")
    print(f"  {coin:9} " + " | ".join(cells))
