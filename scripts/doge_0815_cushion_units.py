"""How the checks' cushion read is made, step by step, on the DOGE KXDOGE15M-26NOV240815 YES bet (user, 2026-11-24: "I don't know what the index-strike value means ... how do you compare it to the cushion read?"). Read-only; run on the host: scripts/cloud.sh run scripts/doge_0815_cushion_units.py
cushion read = ln(blend / strike) / (u15 x sqrt(minutes left)), where blend = the mean of the index's last 1, 3 and 5 minute averages (binaryrisk.readings) and u15 = the typical minute (root-mean-square one-tick log move per minute over the last 15 minutes). Columns: the instant gap in typical minutes (ln(index/strike)/u15), the blended gap in typical minutes, the minutes left and their square root (the divisor), and the read."""
import bisect
import math

from kalshi_moonshot import config, db

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
w = st.execute("SELECT close_ts, strike FROM windows WHERE ticker='KXDOGE15M-26NOV240815-15'").fetchone()
close, strike = float(w["close_ts"]), float(w["strike"])
opened = close - 900.0
tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id='DOGEUSD_RTI' AND ts BETWEEN ? AND ? ORDER BY ts", (opened - 4000.0, close)).fetchall()
T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
print(f"strike {strike}")
print(f"{'sec':>4s} {'index':>9s} {'u15 %/min':>10s} {'inst gap':>9s} {'blend gap':>10s} {'min left':>9s} {'sqrt':>6s} {'read':>6s}   (gaps in typical minutes)")
for sec in (120, 180, 240, 300, 360, 390, 420, 480):
    now = opened + sec
    j = bisect.bisect_right(T, now) - 1
    iu = bisect.bisect_left(T, now - 900.0)
    sq = sum(math.log(V[k] / V[k - 1]) ** 2 for k in range(iu + 1, j + 1) if V[k] > 0 and V[k - 1] > 0)
    u15 = math.sqrt(sq / (T[j] - T[iu]) * 60.0)
    av = lambda m: sum(V[bisect.bisect_left(T, now - 60.0 * m): j + 1]) / len(V[bisect.bisect_left(T, now - 60.0 * m): j + 1])  # noqa: E731
    blend = (av(1) + av(3) + av(5)) / 3.0
    lm = (close - now) / 60.0
    inst, bl = math.log(V[j] / strike) / u15, math.log(blend / strike) / u15
    print(f"{sec:4d} {V[j]:9.6f} {100 * u15:10.4f} {inst:+9.2f} {bl:+10.2f} {lm:9.1f} {math.sqrt(lm):6.2f} {bl / math.sqrt(lm):+6.2f}")
