"""The 14:00 ET crypto window of 2026-11-24 (user: "just had a really bad wave on that"): for each PriskP bet, every 30 s: the side's bid, the settlement index against Kalshi's own strike (bp), and the perp against its own value at the window's open (bp, what value_sale_crossed reads). Read-only; run on the host."""
import bisect

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.perpref import index_for_series as perp_for

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
bets = st.execute("SELECT d.ticker, d.side, d.ts, d.exit_ts FROM decisions d WHERE d.lane IN ('priskp','manual') AND d.action='bet' AND d.stake_usd>0 AND d.ticker LIKE '%26NOV241400-00' ORDER BY d.ts").fetchall()
for b in bets:
    w = st.execute("SELECT series, close_ts, strike, result FROM windows WHERE ticker=?", (b["ticker"],)).fetchone()
    close, strike, opened = float(w["close_ts"]), float(w["strike"]), float(w["close_ts"]) - 900.0
    ix, kp = index_for_series(w["series"]), perp_for(w["series"]) or ("KP_" + w["series"][2:-3])
    def ticks(i):
        r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (i, opened - 120.0, close + 5.0)).fetchall()
        return [float(x[0]) for x in r], [float(x[1]) for x in r]
    T, V = ticks(ix)
    PT, PV = ticks(kp)
    p_open = PV[bisect.bisect_left(PT, opened)] if PT else 0.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], opened, close)).fetchall()
    sc = 100.0 if q and max(float(x[2]) for x in q) > 1.5 else 1.0
    qt = [float(x[0]) for x in q]
    print(f"\n{b['ticker'][2:11]} {b['side']} strike {strike} perp_open {p_open} result {w['result']} bought {b['ts'] - opened:.0f}s, sold {'-' if not b['exit_ts'] else format(float(b['exit_ts']) - opened, '.0f') + 's'}")
    line = []
    for sec in range(120, 900, 60):
        now = opened + sec
        j, jq = bisect.bisect_right(T, now) - 1, bisect.bisect_right(qt, now) - 1
        k = bisect.bisect_right(PT, now) - 1
        bid = (float(q[jq][1]) / sc) if b["side"] == "yes" else 1.0 - float(q[jq][2]) / sc
        sg = 1 if b["side"] == "yes" else -1
        line.append(f"{sec // 60:2d}m bid {bid:.2f} idx {sg * (V[j] / strike - 1) * 1e4:+5.1f} perp {sg * (PV[k] / p_open - 1) * 1e4:+5.1f}")
    print("\n".join(line))
