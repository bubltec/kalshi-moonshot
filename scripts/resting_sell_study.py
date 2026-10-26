"""Cash out with a resting sell, or let it ride? Favorites bought at the ask and held to the result, against the same buys with a resting sell order at a set price (no fee when it rests). Replayed from the tape. Places nothing.

Buy: every 30 s from minute `first` to `last` of a 15-minute window, a side with its ask 70-88c and a spread of 4c or less, bought at the ask (the buy fee is paid).
Resting sell at `price` (or the price paid plus `plus`): cautious fill, it fills only if the side's bid reaches it before the close. Unfilled, the bet is held to the result (no fee at settlement).
Returns per dollar paid, standard errors across windows. The last column adds the stop-loss at 60% of the price paid (a sale at the bid with the sale fee) that crypto_favorite runs.
    docker compose exec -T bot python - < scripts/resting_sell_study.py
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
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M",
          "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")
data = []
for m in main.db.execute(f"SELECT ticker, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no')", SERIES).fetchall():
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) >= 60:
        data.append((m, [r[0] for r in q], q))
print(f"{len(data)} windows with a tape")


def run(first, last, sell_at=None, plus=None, stop=False):
    out = []
    for m, qts, q in data:
        close = m["close_ts"]
        for side in ("yes", "no"):
            asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
            bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
            win = 1.0 if m["result"] == side else 0.0
            t = close - 900 + first * 60
            while t < close - 900 + last * 60:
                i = bisect.bisect_right(qts, t) - 1
                t += 30
                if i < 0 or not (0.70 <= asks[i] <= 0.88) or asks[i] - bids[i] > 0.04:
                    continue
                a = asks[i]
                cost = a + fee(a)
                target = sell_at if sell_at is not None else (a + plus if plus is not None else None)
                result = (win - cost) / cost
                if target is not None and target > a:
                    for j in range(i + 1, len(qts)):
                        if qts[j] > close:
                            break
                        if stop and bids[j] <= a * 0.60:
                            result = (bids[j] - fee(bids[j]) - cost) / cost
                            break
                        if bids[j] >= target:
                            result = (target - cost) / cost  # a resting sell pays no fee
                            break
                elif stop:
                    for j in range(i + 1, len(qts)):
                        if qts[j] > close:
                            break
                        if bids[j] <= a * 0.60:
                            result = (bids[j] - fee(bids[j]) - cost) / cost
                            break
                out.append((m["ticker"], result))
    return out


def show(label, rows):
    per = defaultdict(list)
    for t_, r in rows:
        per[t_].append(r)
    mm = [sum(v) / len(v) for v in per.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
    return f"  {label:34} n={len(rows):6} ({len(per):4} windows)  return {statistics.mean(r for _, r in rows):+6.1%} +-{se:.1f}"


for first, last, name in ((1, 9, "scalp period, minutes 1-9"), (5, 12, "the lane's entry window, minutes 5-12")):
    print(f"\\n{name}: favorites at 70-88c")
    print(show("hold to the result", run(first, last)))
    for plus in (0.02, 0.03, 0.05, 0.08):
        print(show(f"resting sell {int(plus * 100)}c above the price paid", run(first, last, plus=plus)))
    for price in (0.92, 0.95, 0.97, 0.99):
        print(show(f"resting sell at {price:.2f}", run(first, last, sell_at=price)))
    print(show("hold, with the 60% stop", run(first, last, stop=True)))
    print(show("sell at 0.97, with the 60% stop", run(first, last, sell_at=0.97, stop=True)))
