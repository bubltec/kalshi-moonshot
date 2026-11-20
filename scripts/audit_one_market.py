"""Audit one market's readings minute by minute (user, 2026-11-20: "something is wrong in the logic", on KXSILVER15M-26NOV181030 bought YES at 0.60 at 10:20 ET, final 45.205 against a 45.273 target). Prints, from Kalshi's perp reference ticks, what the model's readings saw at each minute: the spot, the strike (the spot at the open), the cushion in dollars and in typical moves, the typical minute in dollars, the 5-minute efficiency and the number of ticks it was computed from, the strike crossings so far, the hour place, and the side ask. Run: SPOT=0 scripts/cloud_job.sh scripts/audit_one_market.py"""
import bisect
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as pidx

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
for tk in ("KXSILVER15M-26NOV181030-30",):
    w = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE ticker=?", (tk,)).fetchone()
    close = float(w["close_ts"]); opened = close - 900.0; ix = pidx(w["series"])
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, close - 900 - 4000, close + 5)).fetchall()
    T, V = [float(r[0]) for r in rows], [float(r[1]) for r in rows]
    k = bisect.bisect_left(T, opened); strike = V[k]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, opened - 60, close)).fetchall()
    qt = [float(r[0]) for r in q]; sc = 100.0 if q and max(float(r[2]) for r in q) > 1.5 else 1.0
    print(f"{tk} via {ix}: {len(T)} ticks in the window and the hour before, strike (spot at open) {strike:.3f}, result {w['result']}")
    print(" min  time ET  ticks5m  spot    cushion$  typ.min$  cz     z_tm   eff5  amp15  hpos  crossings  yes_ask")
    side = None
    cross = 0; prev = None
    for tt in [opened + 60.0 * i for i in range(1, 15)]:
        j = bisect.bisect_right(T, tt) - 1
        f = br.readings(T, V, tt, strike, close - tt)
        i5 = bisect.bisect_left(T, tt - 300)
        n5 = j - i5
        # crossings since the open
        c = 0; p = None
        for m in range(bisect.bisect_left(T, opened), j + 1):
            sg = V[m] > strike
            if p is not None and sg != p: c += 1
            p = sg
        qj = bisect.bisect_right(qt, tt) - 1
        ya = float(q[qj][2]) / sc if qj >= 0 else float("nan")
        et = datetime.fromtimestamp(tt - 5 * 3600, timezone.utc).strftime("%H:%M")
        if f:
            typ = V[j] * (abs(f["cz"]) and 0)  # placeholder
            print(f" {int((tt - opened) / 60):3d}  {et}   {n5:6d}  {V[j]:.3f}  {V[j] - strike:+.3f}   {'':8s}  {f['cz']:+.2f}  {f['z_tm']:+.2f}  {f['eff5']:.2f}  {f['amp15']:4.1f}  {f['h_pos']:.2f}  {c:6d}     {ya:.2f}")
        else:
            print(f" {int((tt - opened) / 60):3d}  {et}   {n5:6d}  {V[j]:.3f}  {V[j] - strike:+.3f}   readings unavailable (short or stale history)   crossings {c} yes_ask {ya:.2f}")
