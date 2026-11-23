"""(v2, same day, user: "the wave is not identified by whether it crossed, more the delta in abs(cushion) over the hour") Does a widening spot cushion beat the ask, and does the hour say wave or steady? The wave reading is now hour_net = |cushion now| less |cushion 60 min ago| and hour_swing = highest less lowest |cushion| in the hour (sampled every 30 s, same 15-min unit); each cut in thirds of the bets, before the run. The crossed flag stays in the data but is not used in the cuts.
(v1 text follows)
Does a widening spot cushion beat the ask, and does the hour say wave or steady? (2026-11-19, user: "score the gap against the ask ... we aren't looking at the hour as well. the hour tells us if the cushion was still in the same side of the current target the whole time. if not, we might be in a wave and we should look to take profit where possible")
One question on the same bets as scripts/commodity_spot_cushion_history.py (live commodity bets since SINCE, default 2026-11-09 00:00 ET, spot index, strike, settled result). Spot cushion = sign x (spot - strike)/strike/(unit x sqrt(minutes left)), unit = rms one-second spot move over the 15 minutes before the buy x sqrt(60).
Readings at the buy: widening = cushion now less cushion 15 minutes before, WIDE if in the top third of the bets (cut on these bets before the run); same_side_hour = the spot stayed on the side's winning side of this market's strike every second of the last 60 minutes (else CROSSED = possible wave); shape = spot path efficiency over 15 minutes, TREND above the median, CHOP at or under it.
Per cell: n, win rate, average ask, win rate minus ask (points), net per $100 staked after fees (what actually happened: the bet's own exit or settlement), and the profit-take view: share of bets whose side bid reached the ask + 5c and + 10c at some second after the buy, and of those how many still ended as a loss (a sale there would have kept a gain). Settings (top third, median, 5c/10c) chosen before the run, not tuned.
Run on the host: scripts/cloud.sh run scripts/commodity_cushion_widening_vs_ask.py"""
import datetime, math, os
from kalshi_moonshot import config, db
ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
PFX = {"KXGOLD": "GOLD", "KXSILVER": "SILVER", "KXPLATINUM": "PLATINUM", "KXPALLADIUM": "PALLADIUM", "KXWTI": "WTI", "KXBRENT": "BRENT", "KXNATGAS": "NATGAS", "KXCOPPER": "COPPER"}
N = 3600
def fee(p): return 0.07 * p * (1.0 - p)
rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, d.exit_ts, w.result, w.close_ts, w.strike FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0", (since,)).fetchall()
def grid(idx, t0):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? ORDER BY ts", (idx, t0 - N - 100, t0)).fetchall()
    if len(r) < 600: return None
    ts = [float(x[0]) for x in r]; vs = [float(x[1]) for x in r]
    g, j = [], 0
    for sec in range(int(t0) - N, int(t0) + 1):
        while j + 1 < len(ts) and ts[j + 1] <= sec: j += 1
        g.append(vs[j] if ts[j] <= sec else None)
    return g
bets, nodata = [], 0
for b in rows:
    pre = str(b["ticker"]).split("-")[0].replace("15M", "")
    if pre not in PFX or not b["ask"]: continue
    t0, close, ask, K = float(b["ts"]), float(b["close_ts"]), float(b["ask"]), float(b["strike"])
    g = None
    for ix in ("BQ_" + PFX[pre], "KP_" + PFX[pre]):
        g = grid(ix, t0)
        if g and all(v is not None for v in g): break
        g = None
    if g is None: nodata += 1; continue
    sign = 1.0 if b["side"] == "yes" else -1.0
    seg = g[N - 900:]
    moves = [(seg[i + 1] - seg[i]) / K for i in range(len(seg) - 1)]
    unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60.0)
    path = sum(abs(m) for m in moves)
    if unit <= 0 or path <= 0: nodata += 1; continue
    def z(i):
        return sign * (g[i] - K) / K / (unit * math.sqrt(max((close - (t0 - (N - i))) / 60.0, 0.25)))
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>? AND ts<? AND bid>0 AND ask>0", (b["ticker"], t0, close)).fetchall()
    sb = [float(x[1]) if b["side"] == "yes" else 1.0 - float(x[2]) for x in q]
    top = max(sb) if sb else ask
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None
    actual = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)
    bets.append(dict(won=won, ask=ask, net=actual - ask - fee(ask), widen=z(N) - z(N - 900), same=all(sign * (v - K) > 0 for v in g), hnet=abs(z(N)) - abs(z(0)), hfac=(abs(z(N)) + 0.25) / (abs(z(0)) + 0.25), f15=(abs(z(N)) + 0.25) / (abs(z(N - 900)) + 0.25), hswing=max(abs(z(i)) for i in range(0, N + 1, 30)) - min(abs(z(i)) for i in range(0, N + 1, 30)), eff=abs(seg[-1] - seg[0]) / K / path, top=top))
n = len(bets)
sw = sorted(r["widen"] for r in bets); cutw = sw[2 * n // 3]
se = sorted(r["eff"] for r in bets); cute = se[n // 2]
for r in bets:
    r["wide"] = r["widen"] > cutw; r["trend"] = r["eff"] > cute
print(f"window {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets ({nodata} left out). WIDE = 15-min cushion change over {cutw:+.2f} (top third); TREND = 15-min path efficiency over {cute:.3f} (median). Same side all hour: {sum(r['same'] for r in bets)} of {n}.")
def rep(name, sel):
    if len(sel) < 5: print(f"   {name:44s} n {len(sel):4d} (too few)"); return
    stake = sum(r["ask"] for r in sel); net = sum(r["net"] for r in sel); w = sum(r["won"] for r in sel) / len(sel)
    a = stake / len(sel)
    h5 = [r for r in sel if r["top"] >= r["ask"] + 0.05]; h10 = [r for r in sel if r["top"] >= r["ask"] + 0.10]
    print(f"   {name:44s} n {len(sel):4d} | win {100*w:5.1f}% ask {100*a:4.1f}c excess {100*(w-a):+5.1f} | net {100*net/stake:+6.1f} per $100 | bid hit +5c {100*len(h5)/len(sel):3.0f}% (still lost {sum(not r['won'] for r in h5):3d}) +10c {100*len(h10)/len(sel):3.0f}% (still lost {sum(not r['won'] for r in h10):3d})")
print("\nAll bets"); rep("all", bets)
print("\nBy widening"); rep("WIDE (top third)", [r for r in bets if r["wide"]]); rep("not wide", [r for r in bets if not r["wide"]])
print("\nBy the hour"); rep("same side all hour", [r for r in bets if r["same"]]); rep("crossed the strike in the hour (wave)", [r for r in bets if not r["same"]])
def thirds3(key):
    sv = sorted(r[key] for r in bets); return sv[n // 3], sv[2 * n // 3]
for key, label in (("hfac", "hour_factor = (|cushion| now + 0.25) / (|cushion| 60 min ago + 0.25): over 1 = the cushion grew, under 1 = it shrank (the 0.25 floor is a setting chosen before the run)"), ("f15", "15-min factor, same ratio against 15 min ago"), ("hnet", "hour_net = |cushion| now less 60 min ago (up = the cushion grew over the hour, down = it shrank)"), ("hswing", "hour_swing = highest less lowest |cushion| in the hour (big = a wave)")):
    c = thirds3(key)
    print(f"\n== {label}: cuts {c[0]:+.2f}, {c[1]:+.2f}")
    grp = (("low third", lambda r: r[key] <= c[0]), ("middle third", lambda r: c[0] < r[key] <= c[1]), ("high third", lambda r: r[key] > c[1]))
    for gn, gf in grp: rep(gn, [r for r in bets if gf(r)])
    print("   -- with the 15-min widening")
    for gn, gf in grp:
        rep(f"{gn}, WIDE", [r for r in bets if gf(r) and r["wide"]]); rep(f"{gn}, not wide", [r for r in bets if gf(r) and not r["wide"]])
    print("   -- by shape")
    for gn, gf in grp:
        rep(f"{gn}, TREND", [r for r in bets if gf(r) and r["trend"]]); rep(f"{gn}, CHOP", [r for r in bets if gf(r) and not r["trend"]])
print("DONE")
