"""STRESS TEST of the last-150-seconds buy rule (the side whose exact chance beats its ask by a bar): earlier half against later half, per coin, calibration where it disagrees, and standard errors clustered by 15-minute slot. Original: The settlement-aware chance: Kalshi settles on the AVERAGE of the index over the last 60 seconds, so the chance of finishing above the target can be computed exactly from the spot, the part of that average already locked, and the spot's volatility.
Compared with Kalshi's own price in the last 3 minutes of finished 15-minute crypto windows. Replayed from the tape's 1-second index and quotes. Places nothing.

At s seconds before the close T, with spot X now and per-second volatility sigma (from 10-second returns over the previous 30 minutes):
  s >= 60  the average over [T-60, T] has not started: mean X, variance sigma^2 (s - 60 + 20)         (a = s - 60 seconds until it starts; variance sigma^2 (a + 60/3))
  s <  60  part of it is locked: mean = (locked sum + s * X) / 60, variance sigma^2 s^3 / (3 * 60^2)
  P(finishes above the target) = Phi((mean - target) / sd)
Reported per s: log-loss of that chance against Kalshi's mid (lower is better), and a buy rule: the side whose chance beats its ask by a bar, at the first such second from 150 s to 20 s before the close, held to the result (buy fee paid).
Standard errors across windows.
    docker compose exec -T bot python - < scripts/settlement_average_study.py
"""
import bisect
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
CHECK = (150, 120, 90, 60, 45, 30)


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def ll(p, y):
    p = min(1 - 1e-4, max(1e-4, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


out = {}  # series -> list of per-window dicts
for series in SERIES:
    ix = index_for_series(series)
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    its, ivs = [x[0] for x in r], [x[1] for x in r]

    def val(t, its=its, ivs=ivs):
        i = bisect.bisect_right(its, t) - 1
        return ivs[i] if i >= 0 and t - its[i] < 20 else None

    rows = []
    for w in main.db.execute("SELECT ticker, result, close_ts, strike FROM windows WHERE series=? AND result IN ('yes','no') AND strike IS NOT NULL", (series,)).fetchall():
        T, K = w["close_ts"], float(w["strike"])
        if T - 3600 < its[0]:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"], T - 200, T)).fetchall()
        if len(q) < 20:
            continue
        qts = [x[0] for x in q]
        samples = [val(T - 1800 + 10 * j) for j in range(0, 180 - 15)]
        if None in samples:
            continue
        rets = [math.log(b / a) for a, b in zip(samples, samples[1:], strict=False)]
        sigma = math.sqrt(sum(x * x for x in rets) / len(rets)) / math.sqrt(10)  # per second, log
        y = 1.0 if w["result"] == "yes" else 0.0
        pts = {}
        for sec in range(150, 19, -1):
            t = T - sec
            X = val(t)
            i = bisect.bisect_right(qts, t) - 1
            if X is None or i < 0 or t - qts[i] > 30:
                continue
            sd1 = sigma * X
            if sec >= 60:
                mean, var = X, sd1 ** 2 * (sec - 60 + 20)
            else:
                locked = 0.0
                for u in range(60 - sec):
                    v = val(T - 60 + u)
                    if v is None:
                        locked = None
                        break
                    locked += v
                if locked is None:
                    continue
                mean, var = (locked + sec * X) / 60.0, sd1 ** 2 * sec ** 3 / (3 * 60.0 ** 2)
            p = phi((mean - K) / math.sqrt(max(var, 1e-12)))
            pts[sec] = {"p": p, "bid": q[i][1], "ask": q[i][2], "mid": (q[i][1] + q[i][2]) / 2}
        if pts:
            rows.append({"tk": w["ticker"], "y": y, "pts": pts})
    out[series] = rows
import collections

slot = lambda tk: tk.split("-")[1]  # noqa: E731  the 15-minute slot, shared by the five coins
trades = []
for series, rows in out.items():
    for r in rows:
        for bar in (0.10,):
            for sec in sorted(r["pts"], reverse=True):
                pt = r["pts"][sec]
                e_yes, e_no = pt["p"] - pt["ask"], (1 - pt["p"]) - (1 - pt["bid"])
                if max(e_yes, e_no) >= bar:
                    if e_yes >= e_no:
                        price, win, p_model = pt["ask"], r["y"], pt["p"]
                    else:
                        price, win, p_model = 1 - pt["bid"], 1 - r["y"], 1 - pt["p"]
                    c = price + fee(price)
                    trades.append({"series": series, "slot": slot(r["tk"]), "price": price, "p": p_model, "win": win, "ret": (win - c) / c, "sec": sec})
                    break
trades.sort(key=lambda x: x["slot"])
half = len(trades) // 2


def line(label, g):
    if len(g) < 30:
        return f"  {label:28} only {len(g)}"
    by = collections.defaultdict(list)
    for x in g:
        by[x["slot"]].append(x["ret"])
    mm = [sum(v) / len(v) for v in by.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")
    return (f"  {label:28} {len(g):4} trades in {len(by):3} slots; paid {100 * statistics.mean(x['price'] for x in g):4.1f}c, the model said {100 * statistics.mean(x['p'] for x in g):4.1f}%, won {100 * statistics.mean(x['win'] for x in g):4.1f}%;"
            f" return with fee {100 * statistics.mean(x['ret'] for x in g):+6.1f}% +-{se:.0f} (clustered by slot)")


print("the rule at a 10-point bar, first second from 150 to 20 s before the close")
print(line("all coins", trades))
print(line("earlier half of the slots", trades[:half]))
print(line("later half of the slots", trades[half:]))
for series in SERIES:
    print(line(series[2:5], [x for x in trades if x["series"] == series]))
print("by how much time was left when it fired:")
for lo, hi in ((90, 151), (45, 90), (20, 45)):
    print(line(f"{lo}-{hi - 1} s left", [x for x in trades if lo <= x["sec"] < hi]))
print("by the price paid:")
for lo, hi in ((0.0, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 1.0)):
    print(line(f"paid {int(lo * 100)}-{int(hi * 100)}c", [x for x in trades if lo <= x["price"] < hi]))
