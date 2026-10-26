"""The tails of a 15-minute market, late in the window: are the 1c longshots and the 99c near-certainties priced right? Replayed from the tape. Places nothing.

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


def run(series, label):
    data = []
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
            t = close - 900 + 540
            while t < close - 900 + 840:
                i = bisect.bisect_right(qts, t) - 1
                t += 15
                if i >= 0 and asks[i] - bids[i] <= 0.02:
                    data.append((m["ticker"], asks[i], win))
    print(f"\\n{label}")
    for lo, hi in BANDS:
        g = [(t_, a, w) for t_, a, w in data if lo <= a < hi]
        if len(g) < 200:
            print(f"  ask {lo:.3f}-{hi:.3f}: only {len(g)}")
            continue
        per = defaultdict(list)
        for t_, a, w in g:
            per[t_].append((w - a) / a)
        mm = [sum(v) / len(v) for v in per.values()]
        se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
        ask = statistics.mean(a for _, a, _ in g)
        won = statistics.mean(w for *_, w in g)
        gross = statistics.mean((w - a) / a for _, a, w in g)
        net = statistics.mean((w - a - fee(a)) / (a + fee(a)) for _, a, w in g)
        print(f"  ask {lo:.3f}-{hi:.3f}: n={len(g):6} ({len(per):4} windows)  price {100 * ask:5.2f}c, won {100 * won:5.2f}%  fee-free return {gross:+7.1%} +-{se:.1f}  with fee {net:+7.1%}")


run(CRYPTO, "crypto")
run(OTHER, "FX and commodities")
