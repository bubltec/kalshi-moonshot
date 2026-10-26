"""Near-certain sides (ask 90-99.5c), late in the window, per instrument and earlier half against later half. Original: The tails of a 15-minute market, late in the window: are the 1c longshots and the 99c near-certainties priced right? Replayed from the tape. Places nothing.

Moments: every 15 s from minute 9 to 14 of a window, each side with a spread of 2c or less, bought at the ask and held to the result. Returns per dollar paid, fee-free and with the buy fee. Standard errors across windows.
The question: the price is the market's chance; if the side wins more or less often than its price says, that is the edge (or the cost).
    docker compose exec -T bot python - < scripts/tail_price_study.py
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
BANDS = ((0.005, 0.02), (0.02, 0.05), (0.05, 0.10), (0.90, 0.95), (0.95, 0.98), (0.98, 0.995))


ALL = CRYPTO + OTHER
rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(ALL))}) AND result IN ('yes','no')", ALL).fetchall():
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60:
        continue
    qts = [r[0] for r in q]
    close = m["close_ts"]
    for side in ("yes", "no"):
        asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
        bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
        win = 1.0 if m["result"] == side else 0.0
        t = close - 900 + 540
        while t < close - 900 + 840:
            i = bisect.bisect_right(qts, t) - 1
            t += 15
            if i >= 0 and 0.90 <= asks[i] < 0.995 and asks[i] - bids[i] <= 0.02:
                a = asks[i]
                rows.append({"cl": m["ticker"], "series": m["series"], "t": close, "ret": (win - a - fee(a)) / (a + fee(a)), "gross": (win - a) / a, "won": win, "ask": a})
cut = sorted({r["t"] for r in rows})[len({r["t"] for r in rows}) // 2]


def line(label, g):
    if len({r["cl"] for r in g}) < 40:
        return f"  {label:34} only {len({r['cl'] for r in g})} windows"
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
    return f"  {label:34} windows {len(per):4}  price {100 * statistics.mean(r['ask'] for r in g):5.2f}c won {100 * statistics.mean(r['won'] for r in g):5.2f}%  return with fee {statistics.mean(r['ret'] for r in g):+6.2%} +-{se:.2f}"


print(f"{len(rows):,} near-certain moments (ask 90-99.5c, minute 9-14), the split between halves is at the middle window")
for series in ALL:
    g = [r for r in rows if r["series"] == series]
    print(line(series + " earlier half", [r for r in g if r["t"] <= cut]) + "\n" + line(series + " later half", [r for r in g if r["t"] > cut]))
print()
print(line("crypto, whole", [r for r in rows if r["series"] in CRYPTO]))
print(line("FX and commodities, earlier half", [r for r in rows if r["series"] in OTHER and r["t"] <= cut]))
print(line("FX and commodities, later half", [r for r in rows if r["series"] in OTHER and r["t"] > cut]))
