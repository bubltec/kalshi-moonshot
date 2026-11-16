"""Does the previous hour of markets say what to buy in the next one? (2026-11-16, user: "our goal is to get rid of the price and time lapsed properties in favor of a smarter process"; "maybe we could build a structured buy scenario every 15 mins ... based on the previous hour of ... markets")

For the idea to work the last hour has to carry over. One question, Kalshi's quotes only: every 15-minute window since Monday 2026-11-09, across the live crypto lane's series. In each market, at the first quote from minute 7.5: the LEADER (the side with the mid over 0.50) and the TRAILER, each bought at its ask and held to settlement, fee in.
The previous hour = the four windows before this one, all settled before it opened (so nothing from the window being scored, or after it, is read). Three readings of that hour, each tried as the scenario picker:
  held     the share of its markets in which the minute-7.5 leader went on to win
  flips    the average number of times the favorite changed in a market
  trailer  what buying the trailer returned in that hour
For each, the windows are split by the previous hour's reading and the leader and the trailer are scored in the NEXT window. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/prev_hour_scenario.py
"""
import datetime
from collections import defaultdict

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
win = defaultdict(list)  # close -> (leader ask, trailer ask, leader won, flips)
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close - 5)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    at = next((x for x in q if x[0] >= opened + 450.0), None)
    if at is None or at[0] > opened + 480.0 or at[2] - at[1] > 0.04:
        continue
    mid = (at[1] + at[2]) / 2
    if abs(mid - 0.5) < 0.005:
        continue
    yes_leads = mid > 0.5
    lead_ask, trail_ask = (at[2], 1 - at[1]) if yes_leads else (1 - at[1], at[2])
    if not (0.03 < trail_ask < 0.97 and 0.03 < lead_ask < 0.99):
        continue
    fav, flips = None, 0
    for _, b, a in q:
        m = (b + a) / 2
        now = "yes" if m >= 0.55 else "no" if m <= 0.45 else None
        if now and fav and now != fav:
            flips += 1
        fav = now or fav
    win[close].append((lead_ask, trail_ask, (w["result"] == "yes") == yes_leads, flips))

closes = sorted(win)
net = lambda ask, won: (1.0 if won else 0.0) / (ask + fee(ask)) - 1.0  # noqa: E731
rows = []  # (prev held, prev flips, prev trailer net, this window's markets)
for i, c in enumerate(closes):
    prev = [m for k in closes[max(0, i - 8):i] if c - 3600.0 - 1 <= k < c - 900.0 + 1 for m in win[k]]  # the four windows that closed before this one opened
    if len(prev) < 20:
        continue
    rows.append((sum(1 for m in prev if m[2]) / len(prev), sum(m[3] for m in prev) / len(prev), sum(net(m[1], not m[2]) for m in prev) / len(prev), win[c]))


def line(name, rs):
    ms = [m for r in rs for m in r[3]]
    if len(ms) < 50:
        print(f"  {name:40s} windows {len(rs):3d}")
        return
    n = len(ms)
    print(f"  {name:40s} windows {len(rs):3d}, markets {n:4d} | leader won {sum(1 for m in ms if m[2]) / n:4.0%} at {sum(m[0] for m in ms) / n:.3f}: {sum(net(m[0], m[2]) for m in ms) / n * 100:+6.1f}% | trailer at {sum(m[1] for m in ms) / n:.3f}: {sum(net(m[1], not m[2]) for m in ms) / n * 100:+6.1f}%")


print(f"window: 2026-11-09 00:00 ET on; {len(closes)} quarter-hours with a minute-7.5 reading, {len(rows)} with a full previous hour; {sum(len(v) for v in win.values())} markets")
line("every window", rows)
print("\n1. by the previous hour's share of leaders that held")
for lo, hi, name in ((0, 0.65, "under 65% held (a flipping hour)"), (0.65, 0.75, "65 to 75%"), (0.75, 0.85, "75 to 85%"), (0.85, 1.01, "85% and over (leaders held)")):
    line(name, [r for r in rows if lo <= r[0] < hi])
print("\n2. by the previous hour's flips per market")
for lo, hi, name in ((0, 1.0, "under 1 flip a market"), (1.0, 2.0, "1 to 2"), (2.0, 3.0, "2 to 3"), (3.0, 99, "3 and over")):
    line(name, [r for r in rows if lo <= r[1] < hi])
print("\n3. by what the trailer returned in the previous hour")
for lo, hi, name in ((-9, -0.3, "trailer lost over 30%"), (-0.3, 0.0, "lost up to 30%"), (0.0, 0.3, "made up to 30%"), (0.3, 99, "made over 30%")):
    line(name, [r for r in rows if lo <= r[2] < hi])
print("DONE")
