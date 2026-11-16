"""The spikes that did not come back: what did the longer view look like? (2026-11-16, user, on the quarter of faded spikes that never won back half: "the quarter that don't survive need more analysis. What did their historical view look like? How much of a spike was it? Does the 3 hour view show similar spikes?")

Same events as scripts/spike_fade_round_trip.py: the first spike in each settled market of the live crypto lane's series since Monday 2026-11-09 (a side's mid up 2 typical minutes or more in 90 s), here only the spikes AGAINST the market (the spiking side had led under 20% of the market so far). Outcome: did the faded side's bid later win back half the spike, all of it, and did it win the market.
The longer view is read from the market's Kalshi perp (the tape's KP index, every 30 s) over the 3 hours before the spike:
  size on the 3-hour view   the perp's move over the spike's 90 s, in units of its own typical 90 s move of the last 3 hours
  similar spikes            how many of the 120 back-to-back 90 s stretches of those 3 hours moved as far or further
  the 3-hour trend          the perp's change over the 3 hours, signed by the spike's direction, in the same units (x sqrt(120)): with the trend or against it
and from the series' own earlier markets:
  recent spikes             of the first spikes against the market in this series' markets of the last 3 hours, the share that came back half
Plus the spike's size on the market's own curve (typical minutes of its Kalshi mid). No coin is named. Bands fixed before the run.

Run on the host: scripts/cloud.sh run scripts/spike_failures_history.py
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
STEP, LOOK, SIZE = 5, 90, 2.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
ev = []  # dicts
for ser in series:
    kp = perp_index(ser)
    pr = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, SINCE - 4 * 3600)).fetchall() if kp else []
    pt, pv, last = [], [], -1
    for r in pr:  # one reading every 30 s (the last tick of each)
        b = int(float(r[0]) // 30)
        if b != last:
            pt.append(b * 30.0); pv.append(math.log(float(r[1]))); last = b
        else:
            pv[-1] = math.log(float(r[1]))
    del pr

    def perp(t):
        i = bisect.bisect_right(pt, t) - 1
        return pv[i] if i >= 0 and t - pt[i] < 120 else None

    wins = st.execute("SELECT ticker, close_ts, result FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (ser, SINCE)).fetchall()
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
            g, back = grid[k], mids[k - k0]
            if g is None or back is None or g[1] - g[0] > 0.04:
                continue
            have = [m for m in mids[:k - k0 + 1] if m is not None]
            moves = [b - a for a, b in zip(have, have[1:])]
            if len(moves) < 12:
                continue
            unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60 / STEP)
            rise = mids[k] - back
            if unit <= 0 or abs(rise) < SIZE * unit:
                continue
            up = rise > 0
            fade_ask = (1 - g[0]) if up else g[1]
            if not (0.05 < fade_ask < 0.95):
                continue
            led = sum(1 for m in have if (m > 0.5) == up) / len(have)
            t = opened + k * STEP
            j = bisect.bisect_right(qt, t)
            best = max(((1 - x[2]) if up else x[1] for x in q[j:]), default=0.0)
            fade_mid = (1 - mids[k]) if up else mids[k]
            e = {"ser": ser, "t": t, "led": led, "units": abs(rise) / unit, "half": best >= fade_mid + 0.5 * abs(rise), "all": best >= fade_mid + abs(rise),
                 "won": (w["result"] == "no") if up else (w["result"] == "yes"), "ask": fade_ask, "z": None, "similar": None, "trend": None}
            a, b = perp(t), perp(t - 90)
            hist = [perp(t - 90 - 90 * n) for n in range(121)]
            if a is not None and b is not None and all(h is not None for h in hist):
                past = [hist[n] - hist[n + 1] for n in range(120)]
                rms = math.sqrt(sum(x * x for x in past) / 120)
                if rms > 0:
                    sign = 1.0 if up else -1.0
                    e["z"] = sign * (a - b) / rms
                    e["similar"] = sum(1 for x in past if abs(x) >= abs(a - b))
                    e["trend"] = sign * (hist[0] - hist[120]) / (rms * math.sqrt(120))
            ev.append(e)
            break
    del pt, pv

ev.sort(key=lambda e: e["t"])
by_ser = defaultdict(list)
for e in ev:
    if e["led"] < 0.2:
        prior = [x for x in by_ser[e["ser"]] if e["t"] - 3 * 3600 <= x["t"] < e["t"] - 900]
        e["recent"] = (sum(1 for x in prior if x["half"]) / len(prior)) if len(prior) >= 3 else None
        by_ser[e["ser"]].append(e)
ag = [e for e in ev if e["led"] < 0.2]


def line(name, rs):
    if len(rs) < 30:
        print(f"  {name:34s} n {len(rs):4d}")
        return
    n = len(rs)
    hold = sum((1.0 if r["won"] else 0.0) / (r["ask"] + fee(r["ask"])) - 1.0 for r in rs) / n * 100
    print(f"  {name:34s} n {n:4d} | came back half {sum(1 for r in rs if r['half']) / n:4.0%}, all {sum(1 for r in rs if r['all']) / n:4.0%} | faded side won {sum(1 for r in rs if r['won']) / n:4.0%} at {sum(r['ask'] for r in rs) / n:.3f} | hold {hold:+6.1f}%")


def bands(title, key, cuts):
    print(f"\n{title}")
    line("no reading", [r for r in ag if r[key] is None])
    for lo, hi, name in cuts:
        line(name, [r for r in ag if r[key] is not None and lo <= r[key] < hi])


print(f"window: 2026-11-09 00:00 ET on; {len(ev)} first spikes, {len(ag)} against the market; {sum(1 for r in ag if r['z'] is not None)} of those with 3 hours of the perp before them")
line("all spikes against the market", ag)
bands("1. the spike's size on the market's own curve (typical minutes of its Kalshi mid)", "units", ((2, 2.5, "2 to 2.5"), (2.5, 3, "2.5 to 3"), (3, 4, "3 to 4"), (4, 99, "4 and over")))
bands("2. the spike's size on the 3-hour view (the perp's 90 s move in its own typical 90 s moves; + = the spike's direction)", "z", ((-99, 0.5, "under 0.5 (the perp barely moved)"), (0.5, 1.5, "0.5 to 1.5"), (1.5, 3, "1.5 to 3"), (3, 99, "3 and over")))
bands("3. similar spikes in the last 3 hours (of 120 stretches, how many moved as far)", "similar", ((0, 1, "none: the biggest move in 3 hours"), (1, 6, "1 to 5"), (6, 25, "6 to 24"), (25, 121, "25 and over: an ordinary move")))
bands("4. the 3-hour trend, signed by the spike (+ = the spike goes with the trend)", "trend", ((-99, -1, "strongly against the trend"), (-1, 0, "against"), (0, 1, "with"), (1, 99, "strongly with the trend")))
bands("5. recent spikes in this series (last 3 hours): the share that came back half", "recent", ((0, 0.5, "under half came back"), (0.5, 0.75, "half to three quarters"), (0.75, 1.01, "three quarters and over")))
print("DONE")
