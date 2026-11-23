"""When the spot cushion grew over the hour, what does the 180-minute cushion shape say? (2026-11-19, user: "when cushion grew, we should look at 180 mins and see the cushion shape there")
Same bets, spot cushion and 15-minute unit as scripts/commodity_cushion_widening_vs_ask.py, but the grid is 3 hours. Subset (stated before the run, structural): hour factor (|cushion| now + 0.25)/(|cushion| 60 min ago + 0.25) over 1, bets with 3 hours of spot before the buy.
Three shape readings, each cut in thirds of the subset:
  f180   (|cushion| now + 0.25)/(|cushion| 180 min ago + 0.25)
  peak   when |cushion| peaked in the 3 hours as a share of the way to now (0 = 3 hours ago, 1 = now), sampled every 60 s
  prior  (|cushion| 60 min ago + 0.25)/(|cushion| 180 min ago + 0.25): how the two hours before the last hour moved
Per cell: n, win rate, ask, excess, net per $100 after fees, wide/not wide, and the take-profit view (bid hit +5c / +10c after the buy, and how many of those still lost). Run on the host: scripts/cloud.sh run scripts/commodity_cushion_grew_180shape.py"""
import datetime, math, os
from kalshi_moonshot import config, db
ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
PFX = {"KXGOLD": "GOLD", "KXSILVER": "SILVER", "KXPLATINUM": "PLATINUM", "KXPALLADIUM": "PALLADIUM", "KXWTI": "WTI", "KXBRENT": "BRENT", "KXNATGAS": "NATGAS", "KXCOPPER": "COPPER"}
N = 10800
def fee(p): return 0.07 * p * (1.0 - p)
rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, w.result, w.close_ts, w.strike FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0", (since,)).fetchall()
def grid(idx, t0):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? ORDER BY ts", (idx, t0 - N - 100, t0)).fetchall()
    if len(r) < 600: return None
    ts = [float(x[0]) for x in r]; vs = [float(x[1]) for x in r]
    g, j = [], 0
    for sec in range(int(t0) - N, int(t0) + 1):
        while j + 1 < len(ts) and ts[j + 1] <= sec: j += 1
        g.append(vs[j] if ts[j] <= sec else None)
    return g
bets, nodata, short = [], 0, 0
for b in rows:
    pre = str(b["ticker"]).split("-")[0].replace("15M", "")
    if pre not in PFX or not b["ask"]: continue
    t0, close, ask, K = float(b["ts"]), float(b["close_ts"]), float(b["ask"]), float(b["strike"])
    g = None
    for ix in ("BQ_" + PFX[pre], "KP_" + PFX[pre]):
        g = grid(ix, t0)
        if g and all(v is not None for v in g[-3600:]): break
        g = None
    if g is None: nodata += 1; continue
    if g[0] is None: short += 1; continue
    sign = 1.0 if b["side"] == "yes" else -1.0
    seg = g[N - 900:]
    moves = [(seg[i + 1] - seg[i]) / K for i in range(len(seg) - 1)]
    unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60.0)
    if unit <= 0: nodata += 1; continue
    def a(i): return abs(sign * (g[i] - K) / K / (unit * math.sqrt(max((close - (t0 - (N - i))) / 60.0, 0.25))))
    q = tape.execute("SELECT bid,ask FROM quotes WHERE ticker=? AND ts>? AND ts<? AND bid>0 AND ask>0", (b["ticker"], t0, close)).fetchall()
    sb = [float(x[0]) if b["side"] == "yes" else 1.0 - float(x[1]) for x in q]
    won = b["result"] == b["side"]
    actual = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if b["exit_price"] is not None else (1.0 if won else 0.0)
    pk = max(range(0, N + 1, 60), key=lambda i: a(i))
    bets.append(dict(won=won, ask=ask, net=actual - ask - fee(ask), top=max(sb) if sb else ask, hf=(a(N) + .25) / (a(N - 3600) + .25), f180=(a(N) + .25) / (a(0) + .25), peak=pk / N, prior=(a(N - 3600) + .25) / (a(0) + .25), wide=(a(N) - a(N - 900)) > 0))
print(f"window {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {len(bets)} bets with 3 hours of spot ({short} left out for a short feed, {nodata} no data). Wide here = |cushion| up over 15 min.")
sub = [r for r in bets if r["hf"] > 1.0]
print(f"subset: hour factor over 1 (cushion grew over the hour): {len(sub)} of {len(bets)}")
def rep(name, sel):
    if len(sel) < 5: print(f"   {name:36s} n {len(sel):4d} (too few)"); return
    stake = sum(r["ask"] for r in sel); net = sum(r["net"] for r in sel); w = sum(r["won"] for r in sel) / len(sel); a_ = stake / len(sel)
    h5 = [r for r in sel if r["top"] >= r["ask"] + .05]; h10 = [r for r in sel if r["top"] >= r["ask"] + .10]
    print(f"   {name:36s} n {len(sel):4d} | win {100*w:5.1f}% ask {100*a_:4.1f}c excess {100*(w-a_):+5.1f} | net {100*net/stake:+6.1f} per $100 | +5c {100*len(h5)/len(sel):3.0f}% (lost {sum(not r['won'] for r in h5):3d}) +10c {100*len(h10)/len(sel):3.0f}% (lost {sum(not r['won'] for r in h10):3d})")
rep("whole subset", sub); rep("hour factor 1 or under (the rest)", [r for r in bets if r["hf"] <= 1.0])
for key, label in (("f180", "f180: |cushion| now vs 180 min ago"), ("peak", "peak: when |cushion| peaked (0 = 3 h ago, 1 = now)"), ("prior", "prior: 180 to 60 min ago movement")):
    sv = sorted(r[key] for r in sub); c = (sv[len(sv) // 3], sv[2 * len(sv) // 3])
    print(f"\n== {label}: cuts {c[0]:.2f}, {c[1]:.2f}")
    for nm, f in (("low third", lambda r: r[key] <= c[0]), ("middle third", lambda r: c[0] < r[key] <= c[1]), ("high third", lambda r: r[key] > c[1])):
        rep(nm, [r for r in sub if f(r)]); rep(nm + ", wide", [r for r in sub if f(r) and r["wide"]])
print("DONE")
