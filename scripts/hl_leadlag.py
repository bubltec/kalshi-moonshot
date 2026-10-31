"""Does Hyperliquid's commodity perp move before the Kalshi 15-minute quote? WTI, Brent and gold, on a 1-second grid from the tape. Read-only.

Pairs: KXWTI15M with xyz:CL, KXBRENT15M with xyz:BRENTOIL, KXGOLD15M with xyz:GOLD. The perp rows are fut_1s (the last trade price in the second, and the dollars the aggressors bought and sold in it); the Kalshi side is each market's quote mid on the
same grid. An EVENT is a second in which the perp's price has moved by Z or more of its own usual 3-second move (the std of 3-second changes over the 10 minutes before, so a quiet coin needs a smaller move than a wild one) or the taker
dollars of the last 3 seconds are one-sided by FLOWSHARE or more with at least MINDOLLARS traded. After an event the Kalshi YES mid is followed for 1..15 s: its change is counted in the direction of the perp's move (up for a rise), in cents, and
compared with the same measurement at random seconds of the same markets (the control: what the quote does anyway). A TRADE TEST then buys the side the event favors at the ask LAT s after the event and sells it at the bid HOLD s later (taker fee both ways), against the same
trade in random directions at random seconds: only a gap between the two is an edge. Needs the perp rows and the Kalshi quotes of the same hours: the Mac tape has both once FUT_FLOW_COINS includes the xyz coins.

    docker compose exec -T bot python - < scripts/hl_leadlag.py        (the Mac tape)    or    scripts/cloud.sh run scripts/hl_leadlag.py        (the cloud tape, once it records the coins)
"""
import bisect
import math
import os
import random
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
PAIRS = {"KXWTI15M": "xyz:CL", "KXBRENT15M": "xyz:BRENTOIL", "KXGOLD15M": "xyz:GOLD"}
Z = float(os.environ.get("Z", "2.5"))
FLOWSHARE, MINDOLLARS = float(os.environ.get("FLOWSHARE", "0.85")), float(os.environ.get("MINDOLLARS", "20000"))
HORIZONS = (1, 2, 3, 5, 10, 15)
LAT, HOLDS = int(os.environ.get("LAT", "1")), (3, 10, 30)
HOURS = float(os.environ.get("HOURS", "48"))
rnd = random.Random(5)
since = time.time() - HOURS * 3600


def grid_of(rows, t0, t1, keep=60):
    """last value per second, forward-filled up to `keep` s, as a dict second -> value"""
    out, i, last, last_t = {}, 0, None, None
    rows = sorted(rows)
    for sec in range(t0, t1):
        while i < len(rows) and rows[i][0] <= sec:
            last, last_t = rows[i][1:], rows[i][0]
            i += 1
        if last is not None and sec - last_t <= keep:
            out[sec] = last
    return out


for series, coin in PAIRS.items():
    perp = tape.execute("SELECT ts, px, buy_usd, sell_usd FROM fut_1s WHERE symbol=? AND ts > ? ORDER BY ts", (coin, since)).fetchall()
    if len(perp) < 300:
        print(f"{series}/{coin}: only {len(perp)} seconds of perp trades so far; need a few hours\n")
        continue
    t0, t1 = int(perp[0][0]), int(perp[-1][0])
    pg = grid_of([(r[0], r[1]) for r in perp], t0, t1 + 1, keep=30)
    flow = {int(r[0]): (float(r[2]), float(r[3])) for r in perp}
    tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ticker LIKE ? AND ts > ?", (series + "-%", since)).fetchall()]
    qg = []
    for tk in tickers:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL AND ts > ? ORDER BY ts", (tk, t0 - 5)).fetchall()
        if len(q) >= 30:
            qg.append((grid_of([(r[0], float(r[1]), float(r[2])) for r in q], int(q[0][0]), int(q[-1][0]) + 1, keep=8)))
    if not qg:
        print(f"{series}/{coin}: no Kalshi quotes over the same hours\n")
        continue
    moves3 = [math.log(pg[t] / pg[t - 3]) for t in pg if t - 3 in pg]
    secs = sorted(pg)
    events = []
    for t in secs:
        if t - 3 not in pg or t - 603 < t0:
            continue
        window = [math.log(pg[x] / pg[x - 3]) for x in range(t - 600, t, 3) if x in pg and x - 3 in pg]
        if len(window) < 30:
            continue
        sd = statistics.pstdev(window) or 1e-9
        r = math.log(pg[t] / pg[t - 3])
        b = sum(flow.get(x, (0, 0))[0] for x in range(t - 2, t + 1))
        sl = sum(flow.get(x, (0, 0))[1] for x in range(t - 2, t + 1))
        d = 0
        if abs(r) >= Z * sd:
            d = 1 if r > 0 else -1
        elif b + sl >= MINDOLLARS and max(b, sl) / (b + sl) >= FLOWSHARE:
            d = 1 if b > sl else -1
        if d:
            events.append((t, d))
    # thin out: one event per 10 s
    thin, last = [], -99
    for t, d in events:
        if t - last >= 10:
            thin.append((t, d))
            last = t
    events = thin

    def mid_at(g, t):
        v = g.get(t)
        return None if v is None else (v[0] + v[1]) / 2

    def drift(ts_dirs, h):
        vals = []
        for t, d in ts_dirs:
            for g in qg:
                a, b = mid_at(g, t), mid_at(g, t + h)
                if a is not None and b is not None and 0.05 < a < 0.95:
                    vals.append((b - a) * d * 100)
        return (sum(vals) / len(vals), len(vals)) if vals else (float("nan"), 0)

    ctrl = [(rnd.choice(secs), rnd.choice((-1, 1))) for _ in range(max(len(events) * 8, 400))]
    print(f"== {series} / {coin}: {len(perp)} perp seconds ({(t1 - t0) / 3600:.1f} h), {len(tickers)} markets, {len(events)} events (Z {Z}, flow share {FLOWSHARE:.0%} of at least ${MINDOLLARS:,.0f})")
    print("   Kalshi mid change in the event's direction, cents:  " + "  ".join(f"+{h}s " + (f"{drift(events, h)[0]:+.2f}" + f" (ctrl {drift(ctrl, h)[0]:+.2f})") for h in HORIZONS))

    def trade(ts_dirs, hold):
        pnl = []
        for t, d in ts_dirs:
            for g in qg:
                a, b = g.get(t + LAT), g.get(t + LAT + hold)
                if a and b and 0.05 < (a[0] + a[1]) / 2 < 0.95:
                    if d > 0:
                        buy, sell = a[1], b[0]
                    else:
                        buy, sell = 1.0 - a[0], 1.0 - b[1]
                    if 0.02 <= buy <= 0.98:
                        pnl.append((sell - buy - fee_per_contract(buy) - fee_per_contract(sell)) * 100)
        return (sum(pnl) / len(pnl), len(pnl)) if pnl else (float("nan"), 0)

    print("   trade test, cents per contract after fees and spread (react in %d s):  " % LAT + "  ".join(f"hold {h}s {trade(events, h)[0]:+.2f} (ctrl {trade(ctrl, h)[0]:+.2f}, n {trade(events, h)[1]})" for h in HOLDS))
    print()
print("DONE")
