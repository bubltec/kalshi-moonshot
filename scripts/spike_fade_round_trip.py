"""Fading a spike as a round trip, by the state the market was in before it (2026-11-16, user: "it relies heavily on the previous market state. Are we in a spike that seems unlikely to hold? The doge example would have worked to buy DOWN, then sell it mid market and buy UP").

One question, scored as a round trip, Kalshi's quotes only: in every settled market of the live crypto lane's series since Monday 2026-11-09, the FIRST spike: a side's mid rises 2 typical minutes or more over 90 seconds (the typical minute: root-mean-square 5 s move of the mid from the open to 90 s ago, scaled to a minute; at least 60 s of it). The fade buys the OTHER side at its ask at that second (spread 0.04 or under). Then, from the quotes that follow: did the faded side's bid win back half of the spike, all of it? And what each plan returned, fee on every leg: a resting sale at half the spike back (else held to settlement), at all of it back, and holding.
The state before the spike, from the market's own curve: the share of the market so far that the SPIKING side had led (under 20% = the spike runs against the whole market so far, as DOGE 09:30's did; 80% and over = the leader stretching its lead), and the minutes into the market. No coin is named. The 2 typical minutes, the 90 s, the plans and the bands were fixed before the run.

Run on the host: scripts/cloud.sh run scripts/spike_fade_round_trip.py
"""
import bisect
import datetime
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
STEP, LOOK, SIZE = 5, 90, 2.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
ev = []  # (seconds in, fade ask, fade mid, spike in cents, best later bid of the faded side, faded side won, spiking side's led share, spike in units)
seen = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close - 5)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    qt = [x[0] for x in q]
    grid = []
    for t in range(int(opened), int(close) - 20, STEP):
        i = bisect.bisect_right(qt, t) - 1
        grid.append(None if i < 0 else (q[i][1], q[i][2]))
    mids = [None if g is None else (g[0] + g[1]) / 2 for g in grid]
    k0 = LOOK // STEP
    for k in range(k0 + 12, len(grid)):
        g, back = grid[k], mids[k - k0]
        if g is None or back is None or g[1] - g[0] > 0.04:
            continue
        have = [m for m in mids[:k - k0 + 1] if m is not None]
        moves = [b - a for a, b in zip(have, have[1:])]
        if len(moves) < 12:
            continue
        unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60 / STEP)
        if unit <= 0:
            continue
        rise = mids[k] - back  # + = YES spiked
        if abs(rise) < SIZE * unit:
            continue
        up = rise > 0
        fade_ask = (1 - g[0]) if up else g[1]  # the fade buys NO when YES spiked
        fade_mid = (1 - mids[k]) if up else mids[k]
        if not (0.05 < fade_ask < 0.95):
            continue
        prior = [m for m in mids[:k - k0 + 1] if m is not None]
        led = sum(1 for m in prior if (m > 0.5) == up) / len(prior)
        t = opened + k * STEP
        j = bisect.bisect_right(qt, t)
        best = max(((1 - x[2]) if up else x[1] for x in q[j:]), default=0.0)
        sym = None  # the bounce with a matching stop: out at half the spike back, or at half the spike further against, whichever the bid reaches first
        for x in q[j:]:
            b = (1 - x[2]) if up else x[1]
            if b >= fade_mid + 0.5 * abs(rise):
                sym = min(0.99, fade_mid + 0.5 * abs(rise))
                break
            if b <= fade_mid - 0.5 * abs(rise):
                sym = max(b, 0.0)
                break
        ev.append((k * STEP, fade_ask, fade_mid, abs(rise), best, (w["result"] == "no") if up else (w["result"] == "yes"), led, abs(rise) / unit, sym))
        break


def plan(rs, share):
    tot = 0.0
    for _, a, mid, spike, best, won, _, _, sym in rs:
        cost = a + fee(a)
        if share == 'sym':
            got = (sym - fee(sym)) if sym is not None and sym > 0.01 else (0.0 if sym is not None else (1.0 if won else 0.0))
            tot += got / cost - 1.0
            continue
        tg = None if share is None else min(0.99, mid + share * spike)
        got = (tg - fee(tg)) if tg is not None and best >= tg else (1.0 if won else 0.0)
        tot += got / cost - 1.0
    return tot / len(rs) * 100


def line(name, rs):
    if len(rs) < 30:
        print(f"  {name:34s} n {len(rs):4d}")
        return
    n = len(rs)
    print(f"  {name:34s} n {n:4d} | fade ask {sum(r[1] for r in rs) / n:.3f}, spike {sum(r[3] for r in rs) / n * 100:4.1f}c | won back half {sum(1 for r in rs if r[4] >= r[2] + 0.5 * r[3]) / n:4.0%}, all {sum(1 for r in rs if r[4] >= r[2] + r[3]) / n:4.0%}, faded side won {sum(1 for r in rs if r[5]) / n:4.0%} | net per $: sell at half back {plan(rs, 0.5):+6.1f}%, at all back {plan(rs, 1.0):+6.1f}%, hold {plan(rs, None):+6.1f}%, half back with a matching stop {plan(rs, 'sym'):+6.1f}% (target first {sum(1 for r in rs if r[8] is not None and r[8] >= r[2]) / n:4.0%})")


print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes, {len(ev)} with a spike of {SIZE:g} typical minutes in {LOOK} s")
line("all first spikes", ev)
print("\nby the state before the spike: the share of the market so far the SPIKING side had led")
for lo, hi, name in ((0, 0.2, "under 20% (against the market)"), (0.2, 0.8, "20 to 80% (contested)"), (0.8, 1.01, "80% and over (leader extends)")):
    line(name, [r for r in ev if lo <= r[6] < hi])
print("\nby minutes into the market")
for lo, hi in ((2, 5), (5, 9), (9, 15)):
    line(f"{lo} to {hi} min in", [r for r in ev if lo * 60 <= r[0] < hi * 60])
print("\nspikes against the market (spiking side led under 20%), by minutes into the market")
for lo, hi in ((2, 5), (5, 9), (9, 15)):
    line(f"against, {lo} to {hi} min in", [r for r in ev if r[6] < 0.2 and lo * 60 <= r[0] < hi * 60])
print("\nspikes against the market, by the spike's size in typical minutes")
for lo, hi in ((2, 3), (3, 5), (5, 99)):
    line(f"against, {lo} to {hi} units", [r for r in ev if r[6] < 0.2 and lo <= r[7] < hi])
print("DONE")
