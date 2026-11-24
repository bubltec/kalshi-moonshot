"""The checks' own readings, traced through the window, for the bets the user and I talked about on 2026-11-24 (DOGE KXDOGE15M-26NOV240815 YES; SOL KXSOL15M-26NOV231945 NO; BTC KXBTC15M-26NOV231630 NO). User: "this is where the cushion read needs to be accurate, compare the markets we just talked about, and follow their cushion (which is just the curve math) you will see a difference".
For each market: the strike from the windows table (the market's own), the settlement index, the checks' cushion, trend and history for the bet's side read by priskp.side_readings every 30 s from minute 1 to the close, and the side's bid and ask. Read-only. Run on the host: scripts/cloud.sh run scripts/doge_0815_readings.py"""
import bisect

from kalshi_moonshot import config, db, priskp
from kalshi_moonshot.kalshiws import index_for_series

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
CASES = [("KXDOGE15M-26NOV240815-15", "yes", 180.0), ("KXSOL15M-26NOV231945-45", "no", None), ("KXBTC15M-26NOV231630-30", "no", None)]
for tk_, side, buy_s in CASES:
    w = st.execute("SELECT series, close_ts, strike, result FROM windows WHERE ticker=?", (tk_,)).fetchone()
    if not w:
        print(tk_, "no window row")
        continue
    close, strike = float(w["close_ts"]), float(w["strike"])
    opened = close - 900.0
    ix = index_for_series(w["series"])
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, opened - 4000.0, close)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk_, opened - 60.0, close)).fetchall()
    scale = 100.0 if q and max(float(r[2]) for r in q) > 1.5 else 1.0
    quotes = [(float(r[0]), float(r[1]) / scale, float(r[2]) / scale) for r in q]
    qt = [x[0] for x in quotes]
    k0 = bisect.bisect_left(T, opened)
    print(f"\n{tk_} {side.upper()}  strike {strike}  index at open {V[k0]:.6f}  result {w['result']}  ({ix}, {len(T)} ticks, {len(quotes)} quotes)")
    print(f"{'sec':>4s} {'index':>10s} {'idx-strike':>11s} {'cushion':>8s} {'trend':>7s} {'history':>8s} {'bid':>6s} {'ask':>6s}")
    for sec in range(60, 900, 30):
        now = opened + sec
        j = bisect.bisect_right(T, now) - 1
        r = priskp.side_readings(T, V, quotes, opened, now, strike, close)
        jq = bisect.bisect_right(qt, now) - 1
        yb, ya = (quotes[jq][1], quotes[jq][2]) if jq >= 0 else (None, None)
        sb, sa = (yb, ya) if side == "yes" else ((None if ya is None else 1 - ya), (None if yb is None else 1 - yb))
        c = None if r is None else r[side]
        print(f"{sec:4d} {V[j]:10.6f} {V[j] - strike:+11.6f} " + (f"{c['cushion']:+8.2f} {c['trend']:+7.2f} {c['history']:8.2f}" if c and all(c.get(x) is not None for x in ('cushion', 'trend', 'history')) else f"{'-':>8s} {'-':>7s} {'-':>8s}") + f" {sb if sb is None else round(sb, 2)!s:>6s} {sa if sa is None else round(sa, 2)!s:>6s}")
