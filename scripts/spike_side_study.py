"""When a spike makes a new favorite, which side pays? (2026-11-16, user, on DOGE 09:30 YES bought at the top of an 80-second spike and stopped out three minutes later: "It bought the wrong side tho. At the top of a spike like that you would want the DOWN but since the history shows a likely outcome")

One question, same moments (CLAUDE.md rule 4), read from Kalshi's own quotes only. In every settled market of the live crypto lane's series since Monday 2026-11-09 (the tape ends 2026-11-16 00:31 ET), the FIRST moment, 3 minutes in or later, at which a side enters the lane's band (ask 0.57 to 0.82, spread 0.03 or under) although the OTHER side had been the favorite (mid over 0.50) for at least 80% of the market so far. That is the buy the lane makes today (the new favorite). Against it: the old favorite, bought at its ask at the same second. Both held to settlement, fee in.
The spike: the new favorite's rise over the last 90 seconds in the market's own typical minutes (root-mean-square 5 s move of the mid since the open, scaled to a minute). "Fast" = 2 typical minutes or more. The 80%, the 90 s and the 2 are stated before the run, not fitted.
Shapes: how fast the flip came, and how far into the market. No coin is named.

Run on the host: scripts/cloud.sh run scripts/spike_side_study.py
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

ev = []  # (new favorite's ask, new favorite won, old favorite's ask, seconds in, spike in typical minutes)
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
    grid = []  # (yes bid, yes ask) every STEP seconds from the open
    for t in range(int(opened), int(close) - 10, STEP):
        i = bisect.bisect_right(qt, t) - 1
        grid.append((q[i][1], q[i][2]) if i >= 0 else None)
    mids = [None if g is None else (g[0] + g[1]) / 2 for g in grid]
    yes_fav = 0
    n = 0
    for k, g in enumerate(grid):
        if g is None:
            continue
        if k * STEP >= FROM and n >= FROM // STEP // 2:
            have = [m for m in mids[:k + 1] if m is not None]
            moves = [b - a for a, b in zip(have, have[1:])]
            unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60 / STEP) if moves else 0.0
            hit = None
            for side in ("yes", "no"):
                bid, ask = (g[0], g[1]) if side == "yes" else (1 - g[1], 1 - g[0])
                other_share = (n - yes_fav) / n if side == "yes" else yes_fav / n
                if LO <= ask <= HI and ask - bid <= SPREAD and other_share >= HELD:
                    back = mids[k - LOOK // STEP] if k >= LOOK // STEP else None
                    rise = None if back is None or unit <= 0 else ((mids[k] - back) if side == "yes" else (back - mids[k])) / unit
                    other_ask = (1 - g[0]) if side == "yes" else g[1]
                    hit = (ask, w["result"] == side, other_ask, k * STEP, rise)
            if hit:
                ev.append(hit)
                break
        n += 1
        yes_fav += mids[k] > 0.5


def net(ask, won):
    return (1.0 if won else 0.0) / (ask + fee(ask)) - 1.0


def line(name, rs):
    if not rs:
        print(f"  {name:36s} n    0")
        return
    n = len(rs)
    new = sum(net(r[0], r[1]) for r in rs) / n
    old = sum(net(r[2], not r[1]) for r in rs if 0.03 < r[2] < 0.97) / n
    print(f"  {name:36s} n {n:4d} | new favorite won {sum(1 for r in rs if r[1]) / n:4.0%} at {sum(r[0] for r in rs) / n:.3f}: {new * 100:+6.1f}% per $ | old favorite at {sum(r[2] for r in rs) / n:.3f}: {old * 100:+6.1f}% per $")


print(f"window: 2026-11-09 00:00 ET to the end of the tape; {len(wins)} settled markets, {seen} with quotes, {len(ev)} with a flip into the band after the other side led 80% of the market so far")
print("\n1. all flips: the side the lane buys today (new favorite) against the side that led the market (old favorite)")
line("all", ev)
print("\n2. by how fast the new favorite rose over the last 90 s (typical minutes)")
for lo, hi, name in ((-99, 1.0, "under 1"), (1.0, 2.0, "1 to 2"), (2.0, 3.0, "2 to 3 (fast)"), (3.0, 99, "3 and over (fast)")):
    line(name, [r for r in ev if r[4] is not None and lo <= r[4] < hi])
print("\n3. by minutes into the market")
for lo, hi in ((3, 6), (6, 9), (9, 15)):
    line(f"{lo} to {hi} min", [r for r in ev if lo * 60 <= r[3] < hi * 60])
print("\n4. fast flips (2 and over) by minutes into the market")
for lo, hi in ((3, 6), (6, 9), (9, 15)):
    line(f"fast, {lo} to {hi} min", [r for r in ev if r[4] is not None and r[4] >= FAST and lo * 60 <= r[3] < hi * 60])
print("DONE")
