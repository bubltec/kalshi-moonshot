"""How the spike rule's buys are placed: taking the ask against resting a bid (2026-11-16, user, after the rule's first live bets went one win in four and ETH 15:30 YES filled on a resting bid 4 cents under the ask as the jump faded: "don't cut the stake fix the problem").

One question, same moments (CLAUDE.md rule 4): every 15 seconds since Monday 2026-11-09, each side the live rule would buy (ask 0.40 to 0.95, spread 0.03 or under, 3 minutes in or later, the perp's last 90 s at +1.5 typical moves or more toward the side), held to settlement, fee in, placed three ways:
  take now        bought at the quoted ask at that second (what the study assumed)
  rest, then take the lane's order as it is: a bid one cent under the ask rests 5 seconds; it fills only if the side's ask comes down to it (no fee on that fill); if not, the fresh ask is bought at second 5 unless it has run more than 5 cents over the first ask (then nothing is bought)
  take at +5 s    the fresh ask five seconds later, whatever happened
The resting order's fills and its misses are shown apart. No coin is named. The one cent and the 5 cents are the lane's own settings (buy_rest_below default, buy_slip + buy_retry_chase).

Run on the host: scripts/cloud.sh run scripts/spike_rule_fills.py
"""
import bisect
import datetime
import math
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
Z = ((-99, -1.5, "z under -1.5 (a real move against)"), (-1.5, -0.5, "-1.5 to -0.5"), (-0.5, 0.5, "-0.5 to +0.5 (the perp is still)"), (0.5, 1.5, "+0.5 to +1.5"), (1.5, 3, "+1.5 to +3"), (3, 99, "+3 and over (a real move for it)"))
SIM = ((0, 1, "none as large in 3 hours"), (1, 6, "1 to 5"), (6, 25, "6 to 24"), (25, 121, "25 and over (ordinary)"))
cells = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
unread = total = 0


def add(key, a, won, tk):
    c = cells[key]
    c[0] += 1; c[1] += won; c[2] += a; c[3] += (1.0 if won else 0.0) / (a + fee(a)) - 1.0; c[4].add(tk)


for ser in series:
    kp = perp_index(ser)
    pr = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, SINCE - 4 * 3600)).fetchall() if kp else []
    pt, pv, last = [], [], -1
    for r in pr:
        b = int(float(r[0]) // 15)  # 15 s buckets stamped at their END and holding their last tick: at a moment on the 15 s grid this is the last tick at or before it. Until 2026-11-16 16:20 these were 30 s buckets stamped at their START, so a reading could hold a tick from up to 30 s AFTER the moment: a look-ahead that made every result here too good
        if b != last:
            pt.append((b + 1) * 15.0); pv.append(math.log(float(r[1]))); last = b
        else:
            pv[-1] = math.log(float(r[1]))
    del pr

    def perp(t):
        i = bisect.bisect_right(pt, t) - 1
        return pv[i] if i >= 0 and t - pt[i] < 120 else None

    for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (ser, SINCE)).fetchall():
        close = float(w["close_ts"])
        opened = close - 900.0
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        n = fav = 0
        for t in range(int(opened), int(close) - 15, 15):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0:
                continue
            bid, ask = q[i][1], q[i][2]
            mid = (bid + ask) / 2
            if t - opened >= 180 and n >= 4 and ask - bid <= 0.03 and t < close - 30:
                read = None
                for side in ("yes", "no"):
                    a = ask if side == "yes" else 1 - bid
                    if not (0.40 <= a <= 0.95):
                        continue
                    total += 1
                    if read is None:
                        hist = [perp(t - 90.0 * k) for k in range(122)]
                        read = False
                        if all(h is not None for h in hist):
                            past = [hist[k] - hist[k + 1] for k in range(1, 121)]
                            rms = math.sqrt(sum(x * x for x in past) / 120)
                            if rms > 0:
                                mv = hist[0] - hist[1]
                                read = (mv / rms, sum(1 for x in past if abs(x) >= abs(mv)))
                    if not read:
                        unread += 1
                        continue
                    z = read[0] if side == "yes" else -read[0]
                    won = w["result"] == side
                    led = fav / n if side == "yes" else (n - fav) / n
                    mins = (t - opened) / 60.0
                    if z < 1.5:
                        continue
                    tk = w["ticker"]
                    add(("way", "take now"), a, won, tk)
                    j0 = bisect.bisect_right(qt, t)
                    later = [(x[2] if side == "yes" else 1 - x[1]) for x in q[j0:] if x[0] <= t + 5.0]
                    fresh = later[-1] if later else a
                    add(("way", "take at +5 s"), fresh, won, tk) if 0.02 < fresh < 0.99 else None
                    bid_px = round(a - 0.01, 2)
                    if any(x <= bid_px + 1e-9 for x in later):
                        cells[("way", "rest: the bid filled (the price came back)")][0] += 1
                        c = cells[("way", "rest: the bid filled (the price came back)")]
                        c[1] += won; c[2] += bid_px; c[3] += (1.0 if won else 0.0) / bid_px - 1.0; c[4].add(tk)
                        c = cells[("way", "rest, then take (the lane today)")]
                        c[0] += 1; c[1] += won; c[2] += bid_px; c[3] += (1.0 if won else 0.0) / bid_px - 1.0; c[4].add(tk)
                    elif fresh <= a + 0.05 and 0.02 < fresh < 0.99:
                        add(("way", "rest: missed, the fresh ask taken at +5 s"), fresh, won, tk)
                        add(("way", "rest, then take (the lane today)"), fresh, won, tk)
                    else:
                        add(("way", "rest: missed and the ask ran away (the buy it did not get)"), a, won, tk)
            n += 1
            fav += mid > 0.5
    del pt, pv


def show(title, kind, order=None):
    print(f"\n{title}")
    keys = [k for k in cells if k[0] == kind]
    keys.sort(key=(lambda k: order.index(k[1])) if order else (lambda k: k[1]))
    for k in keys:
        c = cells[k]
        print(f"  {k[1]:52s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%")


print("window: 2026-11-09 00:00 ET on; sides the live spike rule would buy (ask 0.40 to 0.95, z +1.5 and over, minute 3 on)")
show("how the buy is placed", "way")
print("DONE")
