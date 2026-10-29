"""Buy the cheap side when takers pile into it in the last seconds, and fill it after our real latency. The NATGAS finish: 321 and 152 YES contracts taken at 3-5c, then YES at 0.54. Replayed from the tape.

For every settled 15-minute market on the tape, every second between 90 and 3 seconds before the close is checked. The CHEAP side is the side whose ask is CHEAP_ASK (0.12) or less. A spike is
SPIKE or more contracts of that side taken by takers over the last 3 seconds at an average price of CHEAP_ASK or less. The first spike of a market triggers a taker buy LAT seconds
later (our decision-to-fill time: the buys took 1.75 s at the median) at the ask then; if that ask has already run past FILL_CAP (0.25) the ticket is "missed" (the price ran away
before we could buy). Fee included. A ticket wins if its side settles in its favor and pays $1.

    scripts/cloud.sh run scripts/spike_cheap_side_study.py [DAYS=3]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "3"))
CHEAP_ASK, FILL_CAP = 0.12, 0.25
SPIKES = (150, 300, 600)
LATS = (0, 2)

since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()

out = {(sp, lat): {"trig": 0, "bought": 0, "missed": 0, "wins": 0, "pnl": 0.0, "cost": 0.0} for sp in SPIKES for lat in LATS}
markets = 0
for w in wrows:
    close = float(w["close_ts"])
    quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 120, close)).fetchall()]
    flow = [(int(t), float(y or 0), float(n or 0), None if v is None else float(v)) for t, y, n, v in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], int(close - 120), int(close))).fetchall()]
    if len(quotes) < 10 or not flow:
        continue
    markets += 1

    def quote_at(t):
        got = [q for q in quotes if q[0] <= t and t - q[0] <= 6]
        return got[-1] if got else None

    for sp in SPIKES:
        done = {lat: False for lat in LATS}
        for t in range(int(close - 90), int(close - 3)):
            q = quote_at(t)
            if not q:
                continue
            yb, ya = q[1], q[2]
            if ya <= CHEAP_ASK:
                side = "yes"
            elif 1.0 - yb <= CHEAP_ASK:
                side = "no"
            else:
                continue
            vol = 0.0
            for ft, yv, nv, vw in flow:
                if t - 3 < ft <= t and vw is not None:
                    if side == "yes" and vw <= CHEAP_ASK:
                        vol += yv
                    elif side == "no" and vw >= 1.0 - CHEAP_ASK:
                        vol += nv
            if vol < sp:
                continue
            for lat in LATS:
                if done[lat]:
                    continue
                done[lat] = True
                cell = out[(sp, lat)]
                cell["trig"] += 1
                q2 = quote_at(t + lat) or q
                ask = q2[2] if side == "yes" else 1.0 - q2[1]
                if ask > FILL_CAP:
                    cell["missed"] += 1
                    continue
                won = w["result"] == side
                cell["bought"] += 1
                cell["wins"] += 1 if won else 0
                cell["cost"] += ask * 100
                cell["pnl"] += (100.0 if won else 0.0) - ask * 100 - fee_per_contract(ask) * 100

print(f"{markets} settled markets over {DAYS:g} days; spike = contracts of the cheap side (ask <= {CHEAP_ASK:.2f}) taken in 3 s; bought at the ask LAT seconds later if it is <= {FILL_CAP:.2f}")
print(f"{'spike':>6} {'LAT':>4} {'triggers':>8} {'ran away':>8} {'bought':>7} {'wins':>5} {'win%':>6} {'avg price':>9} {'P&L/ticket':>11} {'ROI':>7}", flush=True)
for sp in SPIKES:
    for lat in LATS:
        c = out[(sp, lat)]
        if not c["bought"]:
            print(f"{sp:>6} {lat:>3}s {c['trig']:>8} {c['missed']:>8} {0:>7}")
            continue
        n = c["bought"]
        print(f"{sp:>6} {lat:>3}s {c['trig']:>8} {c['missed']:>8} {n:>7} {c['wins']:>5} {c['wins'] / n:>6.1%} {c['cost'] / n:>8.1f}c {c['pnl'] / n:>+10.2f}c {c['pnl'] / c['cost']:>+7.0%}")
