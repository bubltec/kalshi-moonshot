"""Feed jump -> Kalshi profit jump. A jump in the underlying's price over a few seconds, as a share of its price, near the strike, split by the trend. Places nothing.

For each 15-minute window with a price feed on the tape (Hyperliquid for commodities and EUR/GBP/JPY, Kalshi's index for crypto), an event is a moment when the feed
moved at least `Z` of its typical 4-second move (its 15-minute volatility scaled down) within 4 s, while the contract is near the money (the spot within about 0.7 of
the remaining volatility of the strike) so that a feed move actually changes the contract's odds. An up-jump favors YES, a down-jump favors NO. We buy the favored side at
its ask two seconds later and measure the sale at the bid after 5-60 s, net of the spread and both fees, beside the same trade at random near-the-money moments in
random directions. Trend: the feed's move over the previous 5 minutes, "with" if it points the way of the jump, "against" if not.
    docker compose exec -T bot python - < scripts/feed_jump_study.py
"""
import bisect
import math
import random
from collections import defaultdict

from kalshi_moonshot import config, db, grid
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
feats = grid.Features(tape)
SER = [x for x in grid.SOURCES]
wins = st.db.execute("select ticker, series, close_ts, strike from windows where result in ('yes','no') and strike is not null and series in (%s)" % ",".join("'%s'" % x for x in SER)).fetchall()
HOLDS = (5, 15, 30, 60)
W, DELAY, CUSH = 4, 2, 0.01
random.seed(4)


def feed_series(series, lo, hi):
    kind, key = grid.SOURCES[series]
    if kind == "index":
        rows = tape.execute("select ts, value from index_ticks where index_id=? and ts>=? and ts<=? order by ts", (key, lo, hi)).fetchall()
    else:
        rows = tape.execute("select ts, px from feed_ticks where symbol=? and ts>=? and ts<=? order by ts", (key, lo, hi)).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def px_at(ft, fp, t):
    i = bisect.bisect_right(ft, t) - 1
    return fp[i] if i >= 0 and t - ft[i] < 15 else None


def trade(qt, qb, qa, side, t_buy, result_unused=None):
    i = bisect.bisect_right(qt, t_buy) - 1
    if i < 0 or t_buy - qt[i] > 5:
        return None
    ask = qa[i] if side == "yes" else 1 - qb[i]
    if not 0.05 <= ask <= 0.95:
        return None
    cost = ask + fee(ask)
    out = {"ask": ask}
    for h in HOLDS:
        if t_buy + h > qt[-1] - 3:
            out[h] = None
            continue
        j = bisect.bisect_right(qt, t_buy + h) - 1
        b = qb[j] if side == "yes" else 1 - qa[j]
        b = max(b - CUSH, 0)
        out[h] = (b - fee(b)) / cost - 1
    j = bisect.bisect_right(qt, t_buy + 10) - 1
    mid0 = (qb[i] + qa[i]) / 2 if side == "yes" else 1 - (qb[i] + qa[i]) / 2
    mid1 = (qb[j] + qa[j]) / 2 if side == "yes" else 1 - (qb[j] + qa[j]) / 2
    out["mid_move_10s"] = (mid1 - mid0) * 100
    return out


events = defaultdict(list)  # key -> list of (window, outcome dict)
control = []
for z_thr in (2.0, 3.0, 4.0):
    pass
for r in wins:
    close = float(r["close_ts"]); start = close - 900
    sig15 = feats._scale(r["series"], 900.0)
    if not sig15:
        continue
    q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (r["ticker"],)).fetchall()
    if len(q) < 60 or q[0][0] > start + 120:
        continue
    qt, qb, qa = [x[0] for x in q], [x[1] for x in q], [x[2] for x in q]
    ft, fp = feed_series(r["series"], start - 400, close)
    if len(ft) < 30:
        continue
    sd4 = sig15 * math.sqrt(W / 900.0)
    last = {2.0: -1e9, 3.0: -1e9, 4.0: -1e9}
    near_times = []
    for t in range(int(start) + 90, int(close) - 90):
        now, then = px_at(ft, fp, t), px_at(ft, fp, t - W)
        if not now or not then:
            continue
        left = (close - t) / 60.0
        sd_left = sig15 * math.sqrt(max(left, 0.25) / 15.0)
        x = math.log(now / r["strike"]) / sd_left
        if abs(x) > 0.7:
            continue
        near_times.append(t)
        move = math.log(now / then)
        before = px_at(ft, fp, t - 300)
        trend = (math.log(now / before) if before else 0.0)
        for zt in (2.0, 3.0, 4.0):
            if abs(move) >= zt * sd4 and t - last[zt] >= 30:
                last[zt] = t
                side = "yes" if move > 0 else "no"
                res = trade(qt, qb, qa, side, t + DELAY, None)
                if res:
                    aligned = (trend > 0) == (move > 0)
                    events[(zt, "with the trend" if aligned else "against the trend")].append((r["ticker"], res))
                    events[(zt, "all")].append((r["ticker"], res))
    for t in random.sample(near_times, min(6, len(near_times))):
        side = random.choice(("yes", "no"))
        res = trade(qt, qb, qa, side, t + DELAY, None)
        if res:
            control.append((r["ticker"], res))


def summ(rows, key):
    by = defaultdict(list)
    for tk, res in rows:
        v = res.get(key)
        if v is not None:
            by[tk].append(v)
    means = [sum(v) / len(v) for v in by.values()]
    if len(means) < 8:
        return None
    n = sum(len(v) for v in by.values())
    m = sum(sum(v) for v in by.values()) / n
    gm = sum(means) / len(means)
    se = math.sqrt(sum((x - gm) ** 2 for x in means) / (len(means) - 1) / len(means))
    return n, m, se


print(f"{len(wins)} windows with a feed; control = {len(control)} random near-the-money moments (random side)\n")
print("return per contract bought on the side the feed jump favors (ask 2 s after the jump, sold at the bid after the hold; spread and both fees paid)\n")
print(f"{'jump size and trend':34}{'events':>8}" + "".join(f"{'sell '+str(h)+'s':>17}" for h in HOLDS) + f"{'Kalshi mid, +10 s':>20}")
for zt in (2.0, 3.0, 4.0):
    for tr in ("all", "with the trend", "against the trend"):
        ev = events.get((zt, tr), [])
        if not ev:
            continue
        row = f"{('%.0f sd, %s' % (zt, tr)):34}{len(ev):8}"
        for h in HOLDS:
            a = summ(ev, h)
            row += (f"{a[1]:+9.1%} ±{a[2]:4.1%}" if a else "-").rjust(17)
        mv = [r[1]["mid_move_10s"] for r in ev]
        row += f"{sum(mv) / len(mv):+19.2f}c"
        print(row)
row = f"{'CONTROL (random, near the money)':34}{len(control):8}"
for h in HOLDS:
    a = summ(control, h)
    row += (f"{a[1]:+9.1%} ±{a[2]:4.1%}" if a else "-").rjust(17)
mv = [r[1]["mid_move_10s"] for r in control]
print(row + f"{sum(mv) / len(mv):+19.2f}c")
