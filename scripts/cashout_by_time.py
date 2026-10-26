"""CASH OUT AND FLIP INTO A CHEAP SIDE (5-25c): the other side is bought only when it costs 5-25c and its settlement-aware chance beats its ask by the bar. Original: CASH OUT AND FLIP ONLY WITH A HIGH CHANCE: the other side is bought only when its settlement-aware chance is at least `min_chance` AND beats its ask by `flip_bar`. Original: CASH OUT AND FLIP: holding a favorite into the last 150 seconds, sell when the settlement-aware chance drops below what the bid pays by `delta`; optionally put the proceeds on the other side. Original: The settlement-aware chance: Kalshi settles on the AVERAGE of the index over the last 60 seconds, so the chance of finishing above the target can be computed exactly from the spot, the part of that average already locked, and the spot's volatility.
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
LO, HI = 0.05, 0.25


def run(delta, flip_bar, min_chance):
    res = []
    for series, rows in out.items():
        for r in rows:
            p0 = r["pts"].get(150) or r["pts"].get(149)
            if not p0:
                continue
            side = "yes" if p0["bid"] >= 0.85 else "no" if (1 - p0["ask"]) >= 0.85 else None
            if side is None:
                continue
            hold = r["y"] if side == "yes" else 1 - r["y"]
            got = None
            for sec in sorted(r["pts"], reverse=True):
                if sec > 149:
                    continue
                pt = r["pts"][sec]
                p_hold = pt["p"] if side == "yes" else 1 - pt["p"]
                bid_hold = pt["bid"] if side == "yes" else 1 - pt["ask"]
                if p_hold < bid_hold - delta:
                    proceeds = bid_hold - fee(bid_hold)
                    ask_o = (1 - pt["bid"]) if side == "yes" else pt["ask"]
                    p_o = 1 - p_hold
                    flip = None
                    if p_o >= min_chance and p_o - ask_o >= flip_bar and LO <= ask_o <= HI:
                        n = proceeds / (ask_o + fee(ask_o))
                        flip = (n * (1 - hold), ask_o, 1 - hold)
                    got = (proceeds, flip, sec)
                    break
            res.append((series, hold, got))
    return res


print("cash out when the held side's settlement-aware chance falls under its bid by delta, by how many seconds were left when it fired (per contract held; positive = cashing out beat holding)")
for delta in (0.10, 0.15):
    r = run(delta, 0.03, 0.0)
    fired = [x for x in r if x[2]]
    for lo, hi in ((100, 150), (60, 100), (30, 60), (0, 30)):
        g = [x for x in fired if lo <= x[2][2] < hi]
        if len(g) >= 8:
            d = [x[2][0] - x[1] for x in g]
            print(f"  delta {delta:.2f}, {lo:3}-{hi - 1:3} s left: {len(g):3} fired; the held side lost {100 * statistics.mean(1 - x[1] for x in g):3.0f}%; cash out minus hold {100 * statistics.mean(d):+6.2f}c +-{100 * statistics.pstdev(d) / math.sqrt(len(d)):.2f}")
