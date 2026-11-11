"""Can Kalshi's perp reference price stand in as the spot for the 15-minute markets? (2026-11-11, user: "i thought we were building a new curve analysis based on kalshi's data only (with kalshi perpetuals to fill real spot pricing compared to strike)")
Question: over every settled 15-minute window since the perp reference feed began recording (KP_*, 2026-11-11 about 07:45 ET), per series:
  ticks: how many KP ticks fall in a window and the median seconds between them (the curve's rate needs a path, not a few points);
  open gap: the KP value at the window open against the market's own strike, in basis points (median and worst): near zero means the perp reference IS the level the strike is set from;
  right side: how often the KP move over the window (close against open) lands on the side that settled, the same for KP against the strike, and the same for the external BiQuote feed's own move, for comparison.
One pass, no settings. Read-only.
    scripts/cloud.sh run scripts/perp_reference_day_check.py
"""

import bisect
import os
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
PAIRS = [("KXGOLD15M", "KP_GOLD", "BQ_GOLD"), ("KXSILVER15M", "KP_SILVER", "BQ_SILVER"), ("KXPLATINUM15M", "KP_PLATINUM", "BQ_PLATINUM"), ("KXPALLADIUM15M", "KP_PALLADIUM", "BQ_PALLADIUM"),
         ("KXCOPPER15M", "KP_COPPER", "BQ_COPPER"), ("KXWTI15M", "KP_WTI", "BQ_WTI"), ("KXBRENT15M", "KP_BRENT", "BQ_BRENT"), ("KXNATGAS15M", "KP_NATGAS", "BQ_NATGAS"), ("KXINX15M", "KP_SPX", "BQ_SPX"),
         ("KXBTC15M", "KP_BTC", "BRTI"), ("KXETH15M", "KP_ETH", "ETHUSD_RTI"), ("KXSOL15M", "KP_SOL", "SOLUSD_RTI")]


def load(ix, t0):
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>=? AND value>0 ORDER BY ts", (ix, t0)).fetchall()
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]


def at(ts, vs, t, tol):
    i = bisect.bisect_right(ts, t) - 1
    return vs[i] if i >= 0 and t - ts[i] <= tol else None


first = tape.execute("SELECT min(ts) FROM index_ticks WHERE index_id='KP_GOLD'").fetchone()[0]
print(f"perp reference ticks begin {first:.0f}")
print(f"{'series':15s} | windows | KP ticks/window, median gap | KP open vs strike bp: median / worst | right side: KP move, KP vs strike, other feed's move (n)")
for ser, kp, other in PAIRS:
    kts, kvs = load(kp, first)
    ots, ovs = load(other, first - 60)
    ws = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE series=? AND close_ts>=? AND result IN ('yes','no') ORDER BY close_ts", (ser, first + 960)).fetchall()
    n = 0
    counts, gaps, offs = [], [], []
    a = b = c = cn = 0
    for w in ws:
        close = float(w["close_ts"])
        op = close - 900.0
        i, j = bisect.bisect_left(kts, op), bisect.bisect_right(kts, close)
        k0, k1 = at(kts, kvs, op + 5, 60), at(kts, kvs, close, 60)
        if k0 is None or k1 is None:
            continue
        n += 1
        counts.append(j - i)
        gaps += [y - x for x, y in zip(kts[i:j], kts[i + 1:j], strict=False)]
        yes = w["result"] == "yes"
        strike = float(w["strike"] or 0.0)
        if strike > 0:
            offs.append((k0 / strike - 1.0) * 1e4)
            b += (k1 > strike) == yes
        a += (k1 > k0) == yes
        o0, o1 = at(ots, ovs, op + 5, 60), at(ots, ovs, close, 60)
        if o0 is not None and o1 is not None:
            cn += 1
            c += ((o1 > strike) == yes) if other.endswith("RTI") or other == "BRTI" else ((o1 > o0) == yes)
    if not n:
        print(f"{ser:15s} | no window with a KP reading at both ends ({len(ws)} settled windows, {len(kts)} KP ticks)")
        continue
    absoff = sorted(abs(x) for x in offs)
    print(f"{ser:15s} | {n:7d} | {statistics.median(counts):5.0f}, {statistics.median(gaps) if gaps else 0:4.1f} s          | {statistics.median(offs) if offs else 0:+8.1f} / {absoff[-1] if absoff else 0:8.1f}                | {a / n:5.0%}, {b / len(offs) if offs else 0:5.0%}, {c / cn if cn else 0:5.0%} ({cn})")
print("DONE")
