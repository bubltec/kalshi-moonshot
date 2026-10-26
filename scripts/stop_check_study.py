"""WHEN SHOULD A TRIGGERED STOP HOLD: replay every finished crypto 15-minute window as a favorite bought at the lane's rules (first quote in minutes 5-12 with a side's ask at 0.76-0.92), then find where the plain stop would fire (the side's bid at or under 55% of the price paid for 10 s).
At that moment compute what the bot knows: the exact settlement chance (settlement.py: spot, locked average, volatility), the spot's gap to the strike in the average's own sigmas, volume bursts against the holder in the last 90 s (ratio against the market's own normal second, share, size), the 10 s trend of the bid in the market's own units, seconds left.
Then score hold-or-sell rules: holding earns (the side's result - the bid) per contract against selling at the bid (0); errors clustered by window.
    docker compose exec -T bot python - < scripts/stop_check_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db, settlement
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
LO, HI, DROP, CONFIRM = 0.76, 0.92, 0.45, 10.0
cases = []
for series in SERIES:
    ix = index_for_series(series)
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    its, ivs = [x[0] for x in r], [x[1] for x in r]
    if not its:
        continue
    for w in main.db.execute("SELECT ticker, result, close_ts, strike FROM windows WHERE series=? AND result IN ('yes','no') AND strike IS NOT NULL", (series,)).fetchall():
        T, K = float(w["close_ts"]), float(w["strike"])
        if T - 3600 < its[0]:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 900, T)).fetchall()
        if len(q) < 60:
            continue
        qts = [x[0] for x in q]
        flow = {int(x[0]): (x[1], x[2]) for x in tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 1100, T)).fetchall()}
        for side in ("yes", "no"):
            entry = None
            for t, bid, ask in q:
                left = T - t
                if not (180 <= left <= 600):
                    continue
                p = ask if side == "yes" else 1 - bid
                if LO <= p <= HI:
                    entry = (t, p)
                    break
            if entry is None:
                continue
            thr = (1 - DROP) * entry[1]
            sbid = [x[1] if side == "yes" else 1 - x[2] for x in q]  # the side's bid
            trig = None
            for i, (t, _b, _a) in enumerate(q):
                if t <= entry[0] or sbid[i] > thr:
                    continue
                j = bisect.bisect_left(qts, t + CONFIRM)
                if j < len(q) and all(sbid[k] <= thr for k in range(i, min(j + 1, len(q)))):
                    trig = (t, sbid[i])
                    break
            if trig is None:
                continue
            t0, bid0 = trig
            left = T - t0
            if left < 10:
                continue
            jx = bisect.bisect_right(its, t0)
            sig = settlement.sigma_per_second(its[:jx], ivs[:jx], t0)
            if sig is None or jx == 0 or t0 - its[jx - 1] > 5:
                continue
            spot = ivs[jx - 1]
            locked = 0.0
            if left < 60:
                locked = settlement.locked_average(its[:jx], ivs[:jx], T, t0)
                if locked is None:
                    continue
            py = settlement.chance_above(K, spot, locked, left, sig)
            if py is None:
                continue
            p_hold = py if side == "yes" else 1 - py
            gap = ((spot - K) if side == "yes" else (K - spot)) / (sig * spot * math.sqrt(max(left, 1.0)))  # sigmas of what is left
            # bursts against the holder in the last 90 s
            best, against_tot = 0.0, 0.0
            for sec in range(int(t0) - 90, int(t0) + 1):
                tot_a = tot_w = 0.0
                for k in range(3):
                    yv, nv = flow.get(sec - k, (0.0, 0.0))
                    a, ww = (yv, nv) if side == "no" else (nv, yv)  # YES takers trade against a NO holder
                    tot_a += a
                    tot_w += ww
                tot = tot_a + tot_w
                per = sorted(sum(flow.get(sec - 3 - k, (0.0, 0.0))) for k in range(120))
                base = (per[59] + per[60]) / 2.0
                ratio = tot / (3 * base + 1)
                if tot and tot_a / tot >= 0.6 and tot_a >= 300 and ratio > best:
                    best = ratio
                against_tot = max(against_tot, tot_a)
            won = 1.0 if w["result"] == side else 0.0
            cases.append({"win": T, "left": left, "bid": bid0, "p": p_hold, "gap": gap, "burst": best, "against": against_tot, "won": won})
print(len(cases), "triggered stops in", len({c["win"] for c in cases}), "windows;", f"{100 * statistics.mean(c['won'] for c in cases):.0f}% of the sides went on to win; the plain stop sells at the bid (0 below)")


def score(name, hold):
    g = [c for c in cases if hold(c)]
    if len(g) < 8:
        print(f"  {name:55} {len(g):4} held (too few)")
        return
    wins = defaultdict(float)
    for c in g:
        wins[c["win"]] += c["won"] - c["bid"]
    tot = sum(c["won"] - c["bid"] for c in g)
    se = math.sqrt(len(wins)) * statistics.pstdev(list(wins.values())) if len(wins) > 1 else float("nan")
    print(f"  {name:55} {len(g):4} held; {100 * statistics.mean(c['won'] for c in g):3.0f}% won; hold minus sell {100 * tot / len(g):+6.1f}c per held contract, total {tot:+6.2f} +-{se:.2f}")


print("rules (hold when ...; positive = holding beat selling)")
score("never hold (plain stop)", lambda c: False)
score("always hold", lambda c: True)
for m in (0.0, 0.05, 0.10, 0.20):
    score(f"exact chance > bid + {m:.2f}", lambda c, m=m: c["p"] > c["bid"] + m)
    score(f"  ... and no burst against in 90 s", lambda c, m=m: c["p"] > c["bid"] + m and c["burst"] == 0)
for g in (0.5, 1.0, 1.5, 2.0):
    score(f"spot gap >= {g:.1f} sigma", lambda c, g=g: c["gap"] >= g)
    score(f"  ... and no burst against in 90 s", lambda c, g=g: c["gap"] >= g and c["burst"] == 0)
print("by seconds left when the stop fires")
for lo, hi in ((10, 60), (60, 120), (120, 240), (240, 600)):
    g = [c for c in cases if lo <= c["left"] < hi]
    if len(g) >= 8:
        print(f"  {lo:3}-{hi - 1:3} s: {len(g):3} stops; {100 * statistics.mean(c['won'] for c in g):3.0f}% won; mean bid {statistics.mean(c['bid'] for c in g):.2f}; mean exact chance {statistics.mean(c['p'] for c in g):.2f}")
