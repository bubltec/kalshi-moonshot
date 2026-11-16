"""Fading a spike only after it has turned (2026-11-16, user: "Yes" to rerunning the fade with the entry moved to after the spike has turned; their hand trade waits until the move levels out).

One question, scored as a round trip, Kalshi's quotes only. As scripts/spike_fade_round_trip.py: the first spike in each settled market of the live crypto lane's series since Monday 2026-11-09 (a side's mid up 2 typical minutes or more in 90 s). New: no buy at first sight. The spiking side's mid is followed to its high; the TURN is the first quote at which it sits one typical minute or more under that high. The fade buys the other side at its ask there (spread 0.04 or under), if the spike has not already given everything back. A spike that never turns is no trade.
Plans, fee on every leg, each with the stop a hand trader would use (out at the bid if the spiking side makes a new high by a cent): a resting sale at half the way back to where the faded side stood before the spike, a resting sale at all the way back, and no target (stop or settlement). Also holding with no stop.
The state before the spike: the share of the market so far the spiking side had led; and the minutes into the market at the buy. No coin is named. The one typical minute for the turn, the stop and the plans were fixed before the run.

Run on the host: scripts/cloud.sh run scripts/spike_turn_fade.py
"""
import bisect
import datetime
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
STEP, LOOK, SIZE, TURN = 5, 90, 2.0, 1.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
ev = []  # (seconds in at the buy, fade ask, way back in cents, led share of the spiking side, half, all, stop only, hold)  the four results are net per $
spikes = never = gone = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close - 5)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    qt = [x[0] for x in q]
    grid = []
    for t in range(int(opened), int(close) - 20, STEP):
        i = bisect.bisect_right(qt, t) - 1
        grid.append(None if i < 0 else (q[i][1], q[i][2]))
    mids = [None if g is None else (g[0] + g[1]) / 2 for g in grid]
    k0 = LOOK // STEP
    for k in range(k0 + 12, len(grid)):
        back = mids[k - k0]
        if grid[k] is None or back is None:
            continue
        have = [m for m in mids[:k - k0 + 1] if m is not None]
        moves = [b - a for a, b in zip(have, have[1:])]
        if len(moves) < 12:
            continue
        unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60 / STEP)
        rise = mids[k] - back
        if unit <= 0 or abs(rise) < SIZE * unit:
            continue
        spikes += 1
        up = rise > 0
        pre = back if up else 1 - back  # the spiking side's level before the spike
        led = sum(1 for m in have if (m > 0.5) == up) / len(have)
        j = bisect.bisect_right(qt, opened + k * STEP)
        ext, entry = (mids[k] if up else 1 - mids[k]), None
        for n in range(j, len(q)):
            sm = (q[n][1] + q[n][2]) / 2 if up else 1 - (q[n][1] + q[n][2]) / 2
            ext = max(ext, sm)
            if sm <= ext - TURN * unit and q[n][2] - q[n][1] <= 0.04:
                entry = (n, sm)
                break
        if entry is None:
            never += 1
            break
        n, sm = entry
        way = sm - pre  # what the faded side still has to win back
        fade_ask = (1 - q[n][1]) if up else q[n][2]
        if way <= 0.02 or not (0.05 < fade_ask < 0.95):
            gone += 1
            break
        fade_mid, stop_at = 1 - sm, (1 - ext) - 0.01
        cost = fade_ask + fee(fade_ask)
        won = (w["result"] == "no") if up else (w["result"] == "yes")
        out = []
        for share in (0.5, 1.0, None):
            tg = None if share is None else min(0.99, fade_mid + share * way)
            got = None
            for x in q[n + 1:]:
                b = (1 - x[2]) if up else x[1]
                if tg is not None and b >= tg:
                    got = tg - fee(tg)
                    break
                if b <= stop_at:
                    got = max(b, 0.0) - (fee(b) if b > 0.01 else 0.0)
                    break
            out.append((got if got is not None else (1.0 if won else 0.0)) / cost - 1.0)
        ev.append((q[n][0] - opened, fade_ask, way, led, out[0], out[1], out[2], (1.0 if won else 0.0) / cost - 1.0))
        break


def line(name, rs):
    if len(rs) < 30:
        print(f"  {name:34s} n {len(rs):4d}")
        return
    n = len(rs)
    avg = lambda i: sum(r[i] for r in rs) / n * 100  # noqa: E731
    print(f"  {name:34s} n {n:4d} | fade ask {sum(r[1] for r in rs) / n:.3f}, way back {sum(r[2] for r in rs) / n * 100:4.1f}c | with the stop: sell half back {avg(4):+6.1f}% (won {sum(1 for r in rs if r[4] > 0) / n:4.0%}), sell all back {avg(5):+6.1f}% (won {sum(1 for r in rs if r[5] > 0) / n:4.0%}), no target {avg(6):+6.1f}% | hold, no stop {avg(7):+6.1f}%")


print(f"window: 2026-11-09 00:00 ET on; {spikes} first spikes; {never} never turned by a typical minute, {gone} had given everything back by the turn, {len(ev)} bought at the turn")
line("all", ev)
print("\nby the state before the spike: the share of the market so far the SPIKING side had led")
for lo, hi, name in ((0, 0.2, "under 20% (against the market)"), (0.2, 0.8, "20 to 80% (contested)"), (0.8, 1.01, "80% and over (leader extends)")):
    line(name, [r for r in ev if lo <= r[3] < hi])
print("\nby minutes into the market at the buy")
for lo, hi in ((2, 5), (5, 9), (9, 15)):
    line(f"{lo} to {hi} min in", [r for r in ev if lo * 60 <= r[0] < hi * 60])
print("\nspikes against the market, by minutes into the market at the buy")
for lo, hi in ((2, 5), (5, 9), (9, 15)):
    line(f"against, {lo} to {hi} min in", [r for r in ev if r[3] < 0.2 and lo * 60 <= r[0] < hi * 60])
print("DONE")
