"""Audit one market's readings minute by minute (user, 2026-11-20: "something is wrong in the logic", on KXSILVER15M-26NOV181030 bought YES at 0.60 at 10:20 ET, final 45.205 against a 45.273 target). Prints, from Kalshi's perp reference ticks, what the model's readings saw at each minute: the spot, the strike (the spot at the open), the cushion in dollars and in typical moves, the typical minute in dollars, the 5-minute efficiency and the number of ticks it was computed from, the strike crossings so far, the hour place, and the side ask. Run: SPOT=0 scripts/cloud_job.sh scripts/audit_one_market.py"""
import bisect
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as pidx

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
import os
for tk in (os.environ.get("TICKER", "KXSILVER15M-26NOV181030-30"),):
    w = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE ticker=?", (tk,)).fetchone()
    close = float(w["close_ts"]); opened = close - 900.0; ix = pidx(w["series"])
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, close - 900 - 4000, close + 5)).fetchall()
    T, V = [float(r[0]) for r in rows], [float(r[1]) for r in rows]
    k = bisect.bisect_left(T, opened); strike = V[k]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, opened - 60, close)).fetchall()
    qt = [float(r[0]) for r in q]; sc = 100.0 if q and max(float(r[2]) for r in q) > 1.5 else 1.0
    print(f"{tk} via {ix}: {len(T)} ticks in the window and the hour before, strike (spot at open) {strike:.3f}, result {w['result']}")
    print("  time ET  spot       vs strike  cz1    cz3    cz5    tr13   yes_ask")
    for tt in [close - 300 + 15.0 * i for i in range(0, 21)]:
        j = bisect.bisect_right(T, tt) - 1
        f = br.readings(T, V, tt, strike, close - tt)
        qj = bisect.bisect_right(qt, tt) - 1
        ya = float(q[qj][2]) / sc if qj >= 0 else float("nan")
        et = datetime.fromtimestamp(tt - 5 * 3600, timezone.utc).strftime("%H:%M:%S")
        if f: print(f"  {et}  {V[j]:.5f}  {V[j] - strike:+.5f}   {f['cz1']:+.2f}  {f['cz3']:+.2f}  {f['cz5']:+.2f}  {f['tr13']:+.2f}   {ya:.2f}")
        else: print(f"  {et}  {V[j]:.5f}  {V[j] - strike:+.5f}   readings unavailable")
