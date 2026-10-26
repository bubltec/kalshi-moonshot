"""Same as theta_forecast_study.py but only sides priced 55-92c, at least one third of the market elapsed (5 min of 15), grouped by price band x direction shown so far.

Original doc: Does the trend of theta (a market speeding up or slowing down) help forecast what a favorite does next? Replayed from the tape. Places nothing.

theta     the shortest window in which the book updated 8 times (2 s ... 10 min); small = busy.
theta trend  r = theta now / theta three thetas ago. r <= 0.67: the market is speeding up; r >= 1.5: slowing down; otherwise steady.
price trend  z, as in theta_trend_study.py: the mid's change over three thetas in units of its normal move; 'up' is z >= 1.
Every 30 s, for each market side whose ask is 70-90c (book within 8c) and active, one sample. For each cell (theta trend x price trend):
  next 2 min   the mid's change over the next two minutes, in cents
  taker        buy at the ask, fee at purchase, hold to the result
  maker        post at the bid, no fee; filled if the ask falls to it within 3 minutes (the market trades through us); hold to the result; per fill and per sample (unfilled = 0)
Standard errors across clusters (a 15-minute slot across coins, or one match).
    docker compose exec -T bot python - < scripts/theta_forecast_study.py
"""
import bisect
import math
import statistics
import time
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
GRID = (2, 4, 6, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 600)
N_UPDATES, K, STEPS, FLOOR, SAMPLE, AHEAD, WAIT = 8, 3, 8, 0.005, 30.0, 120.0, 180.0
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
FX = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
COMMODITY = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")


def theta_at(ts, t):
    for w in GRID:
        if bisect.bisect_right(ts, t) - bisect.bisect_right(ts, t - w) >= N_UPDATES:
            return float(w)
    return None


def mid_at(ts, mids, t):
    i = bisect.bisect_right(ts, t) - 1
    return mids[i] if i >= 0 else None


def trend_z(ts, mids, t, th):
    if ts[0] > t - (K + 1) * th:
        return None
    m = [mid_at(ts, mids, t - i * th) for i in range(STEPS + 1)]
    d = [m[i] - m[i + 1] for i in range(STEPS) if m[i] is not None and m[i + 1] is not None]
    if len(d) < 5:
        return None
    return (m[0] - m[K]) / (max(FLOOR, math.sqrt(sum(x * x for x in d) / len(d))) * math.sqrt(K))


def samples(q, win_yes, cluster, start=None, end=None):
    out = []
    ts = [r[0] for r in q]
    for side in ("yes", "no"):
        bid = [r[1] if side == "yes" else 1 - r[2] for r in q]
        ask = [r[2] if side == "yes" else 1 - r[1] for r in q]
        mids = [(b + a) / 2 for b, a in zip(bid, ask, strict=True)]
        win = win_yes if side == "yes" else not win_yes
        s0, e0 = (ts[0], ts[-1]) if start is None else (start, end)
        t = max(ts[0] + 90, s0 + (e0 - s0) / 3)
        while t < ts[-1] - AHEAD - 20:
            i = bisect.bisect_right(ts, t) - 1
            t += SAMPLE
            a, b = ask[i], bid[i]
            if not (0.55 <= a <= 0.92) or a - b > 0.08 or b <= 0:
                continue
            th = theta_at(ts, ts[i])
            if th is None:
                continue
            z = trend_z(ts, mids, ts[i], th)
            earlier = theta_at(ts, ts[i] - K * th)
            fwd = mid_at(ts, mids, ts[i] + AHEAD)
            if z is None or earlier is None or fwd is None:
                continue
            r = th / earlier
            tc = a + fee(a)
            fill = next((j for j in range(i + 1, bisect.bisect_right(ts, ts[i] + WAIT)) if ask[j] <= b + 1e-9), None)
            out.append({"cl": cluster, "speed": "speeding up" if r <= 0.67 else "slowing down" if r >= 1.5 else "steady", "price": "with" if z >= 1.0 else "against" if z <= -1.0 else "flat", "band": "55-70" if a < 0.70 else "70-92",
                        "fwd": (fwd - mids[i]) * 100, "taker": ((1 - tc) / tc) if win else -1.0, "maker": None if fill is None else ((1 - b) / b if win else -1.0), "theta": th})
    return out


def se(rows, key):
    per = defaultdict(list)
    for r in rows:
        if r[key] is not None:
            per[r["cl"]].append(r[key])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 1 else float("nan")


def show(name, rows):
    print(f"\n{'=' * 124}\n{name}: {len(rows):,} samples, median theta {statistics.median(r['theta'] for r in rows):.0f} s")
    for band in ("55-70", "70-92"):
        for price in ("with", "flat", "against"):
            g = [r for r in rows if r["band"] == band and r["price"] == price]
            if len(g) < 40:
                print(f"  {band}c trend {price:7}: only {len(g)}")
                continue
            f = [r for r in g if r["maker"] is not None]
            print(f"  {band}c trend {price:7} n={len(g):5} ({len({r['cl'] for r in g}):4} clusters) next 2 min {statistics.mean(r['fwd'] for r in g):+5.2f}c +-{se(g, 'fwd'):.2f} | taker {statistics.mean(r['taker'] for r in g):+6.1%} +-{100 * se(g, 'taker'):.1f}"
                  f" | maker fills {len(f) / len(g):.0%}, per fill {(statistics.mean(r['maker'] for r in f) if f else float('nan')):+6.1%}, per sample {statistics.mean((r['maker'] if r['maker'] is not None else 0.0) for r in g):+6.1%}")


def run_15m(name, series):
    rows = []
    for r in main.db.execute(f"SELECT ticker, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(series))}) AND result IN ('yes','no')", tuple(series)).fetchall():
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)).fetchall()
        if len(q) >= 60:
            rows += samples(q, r["result"] == "yes", r["ticker"].split("-")[1], r["close_ts"] - 900, r["close_ts"])
    show(name, rows)


run_15m("crypto 15-minute", CRYPTO)
run_15m("FX 15-minute", FX)
run_15m("commodity 15-minute", COMMODITY)
for name, prefixes in (("CS2 and Dota 2", ("KXCS2GAME", "KXDOTA2GAME")), ("eSoccer", ("KXESOCCERGAME",))):
    rows = []
    for prefix in prefixes:
        for (tk,) in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 60", (prefix + "-%", time.time() - 1800)).fetchall():
            try:
                res = client.market(tk).get("result")
            except Exception:
                continue
            time.sleep(0.3)
            if res not in ("yes", "no"):
                continue
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
            if len(q) >= 60:
                rows += samples(q, res == "yes", tk.rsplit("-", 1)[0])
    show(name, rows)
