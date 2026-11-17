"""For each live commodity bet since 11-13 11:02 ET: the paper curve lanes' reading of the same market and side nearest the buy (within 30 s): the curve's chance less the ask ("curve edge"). Split at the paper lanes' own bar (-0.05)."""
import datetime, re, sys, traceback
from kalshi_moonshot import config, db
sys.excepthook = lambda *e: print(''.join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load(); st = db.connect(s.db_path, "main", shared=True)
since = datetime.datetime(2026, 11, 13, 11, 2, tzinfo=ET).timestamp()
res = {w["ticker"]: w["result"] for w in st.execute("SELECT ticker, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (since,)).fetchall()}
bets = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts FROM decisions WHERE lane LIKE 'commodity%' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (since,)).fetchall()
out = []
for b in bets:
    near = st.execute("SELECT ts, reason, action FROM decisions WHERE ticker=? AND side=? AND mode<>'trade' AND lane IN ('commodity_multiscale_kalshi','commodity_multiscale_rest','commodity_multiscale_tick') AND ts BETWEEN ? AND ? ORDER BY abs(ts-?) LIMIT 6", (b["ticker"], b["side"], float(b["ts"]) - 30, float(b["ts"]) + 30, float(b["ts"]))).fetchall()
    edge = None
    for n in near:
        m = re.search(r"curve's chance ([0-9.]+) is ([0-9.]+) under the ask", n["reason"] or "")
        if m:
            edge = -float(m[2]); break
        m = re.search(r"\[curve edge ([+-][0-9.]+)\]", n["reason"] or "")
        if m:
            edge = float(m[1]); break
    won = res.get(b["ticker"])
    if won is None:
        continue
    won = won == b["side"]
    n_ = float(b["contracts"]); stake = float(b["stake_usd"])
    out.append((edge, won, n_ * (1.0 if won else 0.0) - stake, stake, b))
def line(name, rs):
    if not rs: print(f"  {name:38s} n 0"); return
    st_ = sum(r[3] for r in rs); h = sum(r[2] for r in rs)
    print(f"  {name:38s} n {len(rs):3d} | staked ${st_:6.0f} | won {100 * sum(r[1] for r in rs) / len(rs):3.0f}% at {st_ / sum(float(r[4]['contracts']) for r in rs):.3f} paid | held to settlement ${h:+8.2f} ({100 * h / st_:+5.1f}%)")
print(f"live commodity bets since 11-13 11:02 ET, settled: {len(out)}")
line("all", out)
line("no paper reading within 30 s", [r for r in out if r[0] is None])
line("curve edge -0.05 or better (paper bar)", [r for r in out if r[0] is not None and r[0] >= -0.05])
line("curve edge under -0.05 (paper refuses)", [r for r in out if r[0] is not None and r[0] < -0.05])
line("  of those, under -0.08", [r for r in out if r[0] is not None and r[0] < -0.08])
line("curve edge 0 or better", [r for r in out if r[0] is not None and r[0] >= 0])
print("today from 09:20:")
for e, won, h, stake, b in out:
    if float(b["ts"]) > datetime.datetime(2026, 11, 17, 9, 20, tzinfo=ET).timestamp():
        print(f"  {datetime.datetime.fromtimestamp(float(b['ts']), ET):%H:%M} {b['ticker'][:26]:26s} {b['side']:3s} edge {e} {'won' if won else 'LOST'} ${h:+.2f}")
