"""Two of the user's statements about trading these markets by hand, checked on Kalshi's own quotes (2026-11-16):
  "almost every 15min market is going to flip at least once"
  "If you buy at under 20c, and it doesn't go above 50c by 5min mark you need to cut the losses and rebuy the opposite side"

1. Flips: in every settled market of the live crypto lane's series since Monday 2026-11-09, how many times the favorite changed. A side is the favorite once its mid is 0.55 or over and stays it until the other side's mid is 0.55 or over (so a wobble around 0.50 is not a flip).
2. The rule as a round trip, fee on every leg: the first moment in the first three minutes that a side's ask is 0.20 or under (at least 0.03, spread 0.03 or under), bought at the ask. If its bid reaches 0.50 before the mark, it is sold there. If not, at the mark it is sold at its bid and the opposite side is bought at its ask with the same dollars and held to settlement. The mark is read two ways, both stated before the run: 5 minutes into the market, and 5 minutes before the close. Against it: the same buy held to settlement, and the same buy cut at the mark with no rebuy.
No coin is named.

Run on the host: scripts/cloud.sh run scripts/underdog_cut_rebuy.py
"""
import bisect
import datetime
from collections import Counter

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
flips, flips_late = Counter(), Counter()
ev = {300: [], 600: []}  # mark -> (ask, reached 0.50 before the mark, leg 1 net per $, rebuy net per $ or None, hold net per $)
seen = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close - 5)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    fav, n, n_late = None, 0, 0
    for t, bid, ask in q:
        mid = (bid + ask) / 2
        now = "yes" if mid >= 0.55 else "no" if mid <= 0.45 else None
        if now and fav and now != fav:
            n += 1
            n_late += t - opened >= 180
        fav = now or fav
    flips[min(n, 4)] += 1
    flips_late[min(n_late, 4)] += 1
    hit = None
    for i, (t, bid, ask) in enumerate(q):
        if t - opened > 180:
            break
        if ask - bid > 0.03:
            continue
        for side in ("yes", "no"):
            a = ask if side == "yes" else 1 - bid
            if 0.03 <= a <= 0.20:
                hit = (i, side, a)
                break
        if hit:
            break
    if not hit:
        continue
    i, side, a = hit
    cost = a + fee(a)
    won = w["result"] == side
    hold = (1.0 if won else 0.0) / cost - 1.0
    qt = [x[0] for x in q]
    for mark in (300, 600):
        m = bisect.bisect_right(qt, opened + mark)
        path = q[i + 1:m]
        if any((x[1] if side == "yes" else 1 - x[2]) >= 0.50 for x in path):
            ev[mark].append((a, True, (0.50 - fee(0.50)) / cost - 1.0, None, hold))
            continue
        if m <= i + 1 or m >= len(q) + 1:
            continue
        last = q[m - 1]
        b = last[1] if side == "yes" else 1 - last[2]
        opp = (1 - last[1]) if side == "yes" else last[2]
        leg1 = (max(b, 0.0) - (fee(b) if b > 0.01 else 0.0)) / cost - 1.0
        rebuy = ((0.0 if won else 1.0) / (opp + fee(opp)) - 1.0) if 0.02 < opp < 0.99 else 0.0
        ev[mark].append((a, False, leg1, rebuy, hold))

tot = sum(flips.values())
print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes")
print("\n1. how many times the favorite changed in a market (0.55 to count as the favorite)")
print("   whole market:      " + ", ".join(f"{k if k < 4 else '4+'} flips {flips[k] / tot:4.0%}" for k in range(5)) + f" | at least one: {1 - flips[0] / tot:4.0%}")
print("   after minute 3:    " + ", ".join(f"{k if k < 4 else '4+'} flips {flips_late[k] / tot:4.0%}" for k in range(5)) + f" | at least one: {1 - flips_late[0] / tot:4.0%}")
print("\n2. a side bought at 0.20 or under in the first three minutes; out at 0.50 if it gets there by the mark, else cut and the other side bought")
for mark, name in ((300, "mark = 5 minutes into the market"), (600, "mark = 5 minutes before the close")):
    rs = ev[mark]
    n = len(rs)
    hit50 = [r for r in rs if r[1]]
    cut = [r for r in rs if not r[1]]
    leg1 = sum(r[2] for r in rs) / n * 100
    both = sum(r[2] + (r[3] or 0.0) for r in rs) / n * 100
    print(f"  {name}: n {n} | ask {sum(r[0] for r in rs) / n:.3f} | reached 0.50 by the mark {len(hit50) / n:4.0%} | hold to settlement {sum(r[4] for r in rs) / n * 100:+6.1f}% | out at 0.50 or cut at the mark {leg1:+6.1f}% | with the rebuy of the other side {both:+6.1f}% per $ of the first stake")
    if cut:
        print(f"      the {len(cut)} cut: the first leg returned {sum(r[2] for r in cut) / len(cut) * 100:+6.1f}%, the rebuy won {sum(1 for r in cut if r[3] > 0) / len(cut):4.0%} and returned {sum(r[3] for r in cut) / len(cut) * 100:+6.1f}% per $")
print("DONE")
