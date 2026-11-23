"""Spot cushion and its history at each live commodity buy, won against lost, at tiers 5, 15, 60 and 180 minutes. (2026-11-19, user: "run spot cushion and cushion_z history first, we should look at tiers 5, 15, 60 in retrospect of any point in time ... compare how the bought won and lost markets look")
One question on the same bets as scripts/commodity_cushion_vol_buy_sell.py: every live commodity bet (mode trade, action bet, lane starting commodity, not manual) since SINCE (default 2026-11-09 00:00 ET) with a settled result and the market's spot index on the tape. Result = what actually happened (the bet's own exit with fee on the sale, or settlement), buy fee in.
Spot cushion = sign x (spot - strike) / (spot_unit x sqrt(minutes left)), sign +1 for YES, -1 for NO: how far the spot sits on the side's winning side of the strike, in the spot's own typical minutes still to come. spot_unit for a tier = rms one-second spot move over the tier's minutes before the buy, times sqrt(60), as a share of the strike. The index is the market's own spot (BQ_ if it has ticks around the buy, else KP_); the asset is only used to find its feed, never in a rule.
Per tier T (5, 15, 60 min) and bet, with the unit fixed from the T minutes before the buy:
  now    cushion at the buy
  low    lowest cushion in the T minutes before the buy (below 0 = the spot was on the wrong side of the strike at some point)
  change cushion now less cushion T minutes before the buy (history: widening or shrinking)
  eff    spot path efficiency over T (net move / summed one-second moves): trending against choppy, the curve shape
Output: (1) won against lost: mean, gap in sd, per tier and reading; (2) the median cushion trajectory (60, 30, 15, 10, 5, 2, 1, 0 min before the buy) for won and for lost bets; (3) each reading in thirds of the bets with n, win rate, net per $100 after fees; (4) cushion 'now' thirds split by efficiency (trending / choppy) at the same tier, so the result is by curve shape. Thirds are cut on these bets, before the run; no other settings were tuned.
Run on the host: scripts/cloud.sh run scripts/commodity_spot_cushion_history.py"""
import datetime, math, os
from kalshi_moonshot import config, db
ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
PFX = {"KXGOLD": "GOLD", "KXSILVER": "SILVER", "KXPLATINUM": "PLATINUM", "KXPALLADIUM": "PALLADIUM", "KXWTI": "WTI", "KXBRENT": "BRENT", "KXNATGAS": "NATGAS", "KXCOPPER": "COPPER"}
N = 10800  # seconds of spot grid before the buy (3 hours)
TIERS = (5, 15, 60, 180)
TRAJ = (180, 120, 60, 30, 15, 10, 5, 2, 1, 0)
def fee(p): return 0.07 * p * (1.0 - p)
rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, w.result, w.close_ts, w.strike FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0", (since,)).fetchall()
def spot_grid(idx, t0):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? ORDER BY ts", (idx, t0 - N - 100, t0)).fetchall()
    if len(r) < 600: return None
    ts = [float(x[0]) for x in r]; vs = [float(x[1]) for x in r]
    g, j = [], 0
    for sec in range(int(t0) - N, int(t0) + 1):
        while j + 1 < len(ts) and ts[j + 1] <= sec: j += 1
        g.append(vs[j] if ts[j] <= sec else None)
    return g  # g[i] = spot at t0-N+i, i=N is the buy
bets, nodata = [], 0
for b in rows:
    pre = str(b["ticker"]).split("-")[0].replace("15M", "")
    if pre not in PFX or not b["ask"]: continue
    t0, close, ask, K = float(b["ts"]), float(b["close_ts"]), float(b["ask"]), float(b["strike"])
    g = None
    for ix in ("BQ_" + PFX[pre], "KP_" + PFX[pre]):
        g = spot_grid(ix, t0)
        if g and all(v is not None for v in g[-900:]): break
        g = None
    if g is None: nodata += 1; continue
    sign = 1.0 if b["side"] == "yes" else -1.0
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None
    actual = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)
    r = dict(won=won, ask=ask, net=actual - ask - fee(ask), cush={}, vals={})
    def z(i, unit):  # cushion at grid index i (seconds before the buy = 3600-i)
        left = max((close - (t0 - (N - i))) / 60.0, 0.25)
        return sign * (g[i] - K) / K / (unit * math.sqrt(left))
    ok = True
    for T in TIERS:
        a = N - T * 60
        seg = [v for v in g[a:] if v is not None]
        if g[a] is None or len(seg) < T * 60 * 0.98:
            if T <= 15: ok = False; break
            continue  # the feed has less than T minutes before this buy: this tier is left out for the bet
        moves = [(seg[i + 1] - seg[i]) / K for i in range(len(seg) - 1)]
        unit = math.sqrt(sum(m * m for m in moves) / len(moves)) * math.sqrt(60.0)
        if unit <= 0:
            if T <= 15: ok = False; break
            continue
        path = sum(abs(m) for m in moves)
        r["vals"][T] = dict(now=z(N, unit), low=min(z(i, unit) for i in range(a, N + 1, 10) if g[i] is not None), change=z(N, unit) - z(a, unit), eff=abs(seg[-1] - seg[0]) / K / path if path > 0 else 0.0)
    if not ok: nodata += 1; continue
    u15 = math.sqrt(sum(((g[i + 1] - g[i]) / K) ** 2 for i in range(N - 900, N)) / 900) * math.sqrt(60.0)
    r["cush"] = {m: z(N - m * 60, u15) for m in TRAJ if g[N - m * 60] is not None}
    bets.append(r)
n = len(bets)
print(f"window {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets with spot, strike and result ({nodata} left out). Won {sum(r['won'] for r in bets)}, lost {sum(not r['won'] for r in bets)}.")
def rep(name, sel):
    if not sel: print(f"   {name:30s} none"); return
    stake = sum(r["ask"] for r in sel); net = sum(r["net"] for r in sel)
    print(f"   {name:30s} n {len(sel):4d} | win {100*sum(r['won'] for r in sel)/len(sel):5.1f}% | avg ask {100*stake/len(sel):4.1f}c | net {net:+7.2f} ({100*net/stake:+5.1f} per $100)")
def mean(x): return sum(x) / len(x) if x else float("nan")
def sd(x):
    m = mean(x); return math.sqrt(sum((v - m) ** 2 for v in x) / len(x)) if x else float("nan")
print("\n(1) won against lost, mean (gap in sd of all bets)")
for T in TIERS:
    for k in ("now", "low", "change", "eff"):
        bt = [r for r in bets if T in r["vals"]]; allv = [r["vals"][T][k] for r in bt]; w = [r["vals"][T][k] for r in bt if r["won"]]; l = [r["vals"][T][k] for r in bt if not r["won"]]
        print(f"  tier {T:3d} min (n {len(bt)}) {k:7s} won {mean(w):+7.2f}  lost {mean(l):+7.2f}  gap {(mean(w)-mean(l))/sd(allv):+5.2f} sd")
print("\n(2) median spot cushion (15-min unit) by minutes before the buy; sorted bets, middle value")
def med(x):
    x = sorted(x); return x[len(x) // 2] if x else float("nan")
print("  min before buy:    " + "  ".join(f"{m:5d}" for m in TRAJ))
for nm, sel in (("won ", [r for r in bets if r["won"]]), ("lost", [r for r in bets if not r["won"]])):
    print(f"  {nm} (n {len(sel):4d}):   " + "  ".join(f"{med([r['cush'][m] for r in sel if m in r['cush']]):+5.2f}" for m in TRAJ))
def cut3(vals):
    sv = sorted(vals); return sv[len(sv) // 3], sv[2 * len(sv) // 3]
print("\n(3) each reading in thirds of the bets")
for T in TIERS:
    bt = [r for r in bets if T in r["vals"]]
    for k in ("now", "low", "change"):
        c = cut3([r["vals"][T][k] for r in bt])
        print(f" -- tier {T} min {k}: cuts {c[0]:+.2f}, {c[1]:+.2f}")
        rep("low third", [r for r in bt if r["vals"][T][k] <= c[0]]); rep("middle third", [r for r in bt if c[0] < r["vals"][T][k] <= c[1]]); rep("high third", [r for r in bt if r["vals"][T][k] > c[1]])
print("\n(4) cushion now in thirds, by spot path efficiency at the same tier (curve shape)")
for T in TIERS:
    bt = [r for r in bets if T in r["vals"]]
    ce = cut3([r["vals"][T]["eff"] for r in bt]); cc = cut3([r["vals"][T]["now"] for r in bt])
    for sn, ssel in (("choppy (low eff)", lambda r: r["vals"][T]["eff"] <= ce[0]), ("middle eff", lambda r: ce[0] < r["vals"][T]["eff"] <= ce[1]), ("trending (high eff)", lambda r: r["vals"][T]["eff"] > ce[1])):
        print(f" -- tier {T} min, {sn}")
        sub = [r for r in bt if ssel(r)]
        rep("cushion low", [r for r in sub if r["vals"][T]["now"] <= cc[0]]); rep("cushion middle", [r for r in sub if cc[0] < r["vals"][T]["now"] <= cc[1]]); rep("cushion high", [r for r in sub if r["vals"][T]["now"] > cc[1]])
print("DONE")
