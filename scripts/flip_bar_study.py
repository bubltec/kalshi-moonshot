"""How long must the other side have led before a new favorite is a bad buy? (2026-11-16, user, on ZEC 21:30 YES bought at 0.66 at minute 6, two and a half minutes after it took the lead, sold at 0.12: "I don't want another zec".)

The live flip rule skips a favorite before minute 9 only when the other side led 80% or more of the market so far. ZEC's other side had led about 55%. One question, same moments (CLAUDE.md rule 4), Kalshi's own quotes only: in every settled market of the live crypto lane's series since Monday 2026-11-09, at minutes 4, 6, 8, 10 and 12, the favorite when it sits in the band (ask 0.57 to 0.82, spread 0.03 or under), bought at the ask and held, fee included, by the share of the market so far that the OTHER side led. The share bands and the moments are stated before the run.
Shapes: the lead share and the stretch of the market. No coin is named.

Run on the host: scripts/cloud.sh run scripts/flip_bar_study.py
"""
import bisect
import datetime
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
LO, HI, SPREAD, FROM, STEP = 0.57, 0.82, 0.03, 180, 5
HELD, LOOK, FAST = 0.80, 90, 2.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]

ev = []  # (ask, won, seconds in, share of the market so far the other side led)
seen = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    qt = [x[0] for x in q]
    yes_fav = n = 0
    for k, t in enumerate(range(int(opened), int(close) - 10, STEP)):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0:
            continue
        g = (q[i][1], q[i][2])
        if k * STEP in (240, 360, 480, 600, 720) and n >= k // 2:
            for side in ("yes", "no"):
                bid, ask = (g[0], g[1]) if side == "yes" else (1 - g[1], 1 - g[0])
                if LO <= ask <= HI and ask - bid <= SPREAD:
                    ev.append((ask, w["result"] == side, k * STEP, (n - yes_fav) / n if side == "yes" else yes_fav / n))
        n += 1
        yes_fav += (g[0] + g[1]) / 2 > 0.5


def net(ask, won):
    return (1.0 if won else 0.0) / (ask + fee(ask)) - 1.0


print(f"window: 2026-11-09 00:00 ET to the end of the tape; {len(wins)} settled markets, {seen} with quotes, {len(ev)} favorite-in-band moments")
for lo, hi, name in ((240, 480, "minutes 4 and 6"), (480, 600, "minute 8"), (600, 900, "minutes 10 and 12")):
    print(f"\n{name}: by the share of the market so far the other side led")
    for a, b, nm in ((0.0, 0.2, "under 20% (this side led throughout)"), (0.2, 0.5, "20 to 50%"), (0.5, 0.65, "50 to 65% (ZEC 21:30 was here)"), (0.65, 0.8, "65 to 80%"), (0.8, 1.01, "80% and over (the live rule's bar)")):
        rs = [r for r in ev if lo <= r[2] < hi and a <= r[3] < b]
        if not rs:
            print(f"  {nm:40s} n    0")
            continue
        print(f"  {nm:40s} n {len(rs):5d} | won {sum(1 for r in rs if r[1]) / len(rs):4.0%} at {sum(r[0] for r in rs) / len(rs):.3f} | {100 * sum(net(r[0], r[1]) for r in rs) / len(rs):+6.1f}% per $")
print("DONE")
