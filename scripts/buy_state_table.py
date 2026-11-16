"""Which curve states pay at the ask, with no price gate and no time gate? (2026-11-16, user: "ideally, we remove the price gate and time gates and the logic can handle its own ... these are rigid guides that don't flow with the market changes"; "lets get back to our real issue -- the buying")

One table (CLAUDE.md rules 1, 4 and 8), Kalshi's own quotes only: every 15 seconds from two minutes in, in every settled market of the live crypto lane's series since Monday 2026-11-09 (the tape ends 2026-11-16 00:31 ET, then resumes 09:17), each side whose ask is 0.05 to 0.95 with a spread of 0.03 or under, bought at the ask and held to settlement, fee in. The state is two readings of the side's own curve, both in the market's own terms:
  position  where the side's mid sits in its own range since the open (0 = its low, 1 = its high)
  led       the share of the market so far in which the side was the favorite (mid over 0.50)
No price band, no minute band, no coin. Moments overlap inside a market, so each cell also says how many markets it comes from. The cells were fixed before the run.

Run on the host: scripts/cloud.sh run scripts/buy_state_table.py
"""
import bisect
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
STEP = 15
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
POS = ((0.0, 0.2, "at its low"), (0.2, 0.4, "low"), (0.4, 0.6, "middle"), (0.6, 0.8, "high"), (0.8, 1.01, "at its high"))
LED = ((0.0, 0.2, "led under 20%"), (0.2, 0.8, "led 20 to 80%"), (0.8, 1.01, "led 80% and over"))
cell = defaultdict(lambda: [0, 0, 0.0, 0.0, set(), 0, 0.0, 0, 0.0])  # n, won, ask sum, net sum, markets, n early, net early, n late, net late
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
    lo = hi = None
    n = fav = 0
    for t in range(int(opened), int(close) - 15, STEP):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0:
            continue
        bid, ask = q[i][1], q[i][2]
        mid = (bid + ask) / 2
        lo, hi = (mid, mid) if lo is None else (min(lo, mid), max(hi, mid))
        if t - opened >= 120 and n >= 4 and hi > lo and ask - bid <= 0.03:
            for side in ("yes", "no"):
                a = ask if side == "yes" else 1 - bid
                if not (0.05 <= a <= 0.95):
                    continue
                pos = (mid - lo) / (hi - lo) if side == "yes" else (hi - mid) / (hi - lo)
                led = fav / n if side == "yes" else (n - fav) / n
                pk = next(name for x, y, name in POS if x <= pos < y)
                lk = next(name for x, y, name in LED if x <= led < y)
                won = w["result"] == side
                net = (1.0 if won else 0.0) / (a + fee(a)) - 1.0
                c = cell[(lk, pk)]
                c[0] += 1; c[1] += won; c[2] += a; c[3] += net; c[4].add(w["ticker"])
                j = 5 if t - opened < 450 else 7
                c[j] += 1; c[j + 1] += net
        n += 1
        fav += mid > 0.5
print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes")
print("state: led x position | moments (markets) | won | avg ask | held net per $ | first half of the market | second half")
for _, _, lk in LED:
    for _, _, pk in POS:
        c = cell[(lk, pk)]
        if not c[0]:
            continue
        print(f"  {lk:17s} {pk:11s} | {c[0]:6d} ({len(c[4]):4d}) | {c[1] / c[0]:4.0%} | {c[2] / c[0]:.3f} | {c[3] / c[0] * 100:+6.1f}% | {(c[6] / c[5] * 100 if c[5] else 0):+6.1f}% (n {c[5]}) | {(c[8] / c[7] * 100 if c[7] else 0):+6.1f}% (n {c[7]})")
print("DONE")
