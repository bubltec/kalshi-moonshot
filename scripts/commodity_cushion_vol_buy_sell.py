"""Buying and selling pattern of the live commodity bets by cushion and volatility at the buy, no time gate. (2026-11-19, user: "a cushion and volatility check" instead of a time gate; the other session found no support for blocking buys on volatility alone.)
One question on the same bets: every live commodity bet (mode trade, action bet, lane starting commodity, not manual) since SINCE (default 2026-11-09 00:00 ET) with a settled result, the bot's `vol Nx` tag and tape quotes. Result = what actually happened (the bet's own exit with fee on the sale, or settlement); buy fee included.
Readings at the buy, all in the market's own typical minute (rms one-second mid move so far x sqrt(60)): 
  cushion_z = (side bid at buy - 0.5) / (unit x sqrt(minutes left))  -- how far the side sits over the coin-flip line, in the volatility still to come
  rise_u    = (side bid at buy - lowest side bid in the 10 minutes before) / unit -- how far above its recent trough
Each cut in thirds of the bets (a study's cut, chosen after seeing nothing). Reported: n, win rate, net per $100 staked after the fee, by cushion_z third, rise_u third, vol third, and vol x cushion_z; then the sale pattern (seconds left at the sale, exit less ask) for wins and losses.
Run on the host: scripts/cloud.sh run scripts/commodity_cushion_vol_buy_sell.py"""
import datetime, math, os, re
from kalshi_moonshot import config, db
ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
def fee(p): return 0.07 * p * (1.0 - p)
rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.exit_ts, d.reason, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall()
bets, nodata = [], 0
for b in rows:
    m = re.search(r"\[vol ([0-9.]+)x\]", str(b["reason"] or ""))
    if not m or not b["ask"]:
        continue
    t0, close, ask = float(b["ts"]), float(b["close_ts"]), float(b["ask"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], close)).fetchall()
    if len(q) < 120:
        nodata += 1; continue
    ts = [float(x[0]) for x in q]; yb = [float(x[1]) for x in q]; ya = [float(x[2]) for x in q]
    sb = yb if b["side"] == "yes" else [1.0 - a for a in ya]
    mid = [(yb[i] + ya[i]) / 2.0 for i in range(len(ts))]
    ms, j = [], 0
    for sec in range(int(ts[0]), int(t0)):
        while j + 1 < len(ts) and ts[j + 1] <= sec: j += 1
        ms.append(mid[j])
    dd = [ms[i + 1] - ms[i] for i in range(len(ms) - 1)]
    if len(dd) < 120: nodata += 1; continue
    unit = math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0)
    left = max((close - t0) / 60.0, 0.25)
    if unit <= 0: continue
    k = max(i for i in range(len(ts)) if ts[i] <= t0) if ts[0] <= t0 else 0
    prior = [sb[i] for i in range(len(ts)) if t0 - 600 <= ts[i] <= t0]
    bid0 = sb[k]
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None
    ex = float(b["exit_price"]) if sold else None
    actual = (ex - fee(ex)) if sold else (1.0 if won else 0.0)
    bets.append(dict(vol=float(m.group(1)), z=(bid0 - 0.5) / (unit * math.sqrt(left)), rise=(bid0 - min(prior)) / unit if prior else 0.0, ask=ask, won=won, net=actual - ask - fee(ask), sold=sold, left_at_sale=(close - float(b["exit_ts"])) if sold and b["exit_ts"] else None, exit_less_ask=(ex - ask) if sold else None))
def rep(name, sel):
    if not sel: print(f"   {name:34s} none"); return
    st_ = sum(r["ask"] for r in sel)
    print(f"   {name:34s} n {len(sel):4d} | win {100*sum(r['won'] for r in sel)/len(sel):5.1f}% | avg ask {100*st_/len(sel):4.1f}c | net {sum(r['net'] for r in sel):+7.2f} ({100*sum(r['net'] for r in sel)/st_:+5.1f} per $100)")
def thirds(key, label):
    srt = sorted(bets, key=lambda r: r[key]); n = len(srt)
    cuts = [srt[n // 3][key], srt[2 * n // 3][key]]
    print(f"--- by {label}: cuts at {cuts[0]:.2f}, {cuts[1]:.2f}")
    parts = [srt[:n // 3], srt[n // 3:2 * n // 3], srt[2 * n // 3:]]
    for nm, p in zip(("low", "middle", "high"), parts): rep(nm, p)
    return cuts
print(f"window {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {len(bets)} live commodity bets ({nodata} left out, no quotes). Cuts are thirds of these bets, chosen before the run.")
rep("all", bets)
cz = thirds("z", "cushion_z (side bid over 0.5, in volatility left)")
thirds("rise", "rise_u (bid above its 10-minute low, typical minutes)")
vc = thirds("vol", "vol tag")
print("--- vol x cushion_z")
for vn, vsel in (("vol low", lambda r: r["vol"] <= vc[0]), ("vol mid", lambda r: vc[0] < r["vol"] <= vc[1]), ("vol high", lambda r: r["vol"] > vc[1])):
    for cn, csel in (("cush low", lambda r: r["z"] <= cz[0]), ("cush mid", lambda r: cz[0] < r["z"] <= cz[1]), ("cush high", lambda r: r["z"] > cz[1])):
        rep(f"{vn}, {cn}", [r for r in bets if vsel(r) and csel(r)])
print("--- the sale pattern")
for nm, sel in (("won", [r for r in bets if r["won"]]), ("lost", [r for r in bets if not r["won"]])):
    sold = [r for r in sel if r["sold"]]
    if sold:
        sl = sorted(r["left_at_sale"] for r in sold if r["left_at_sale"] is not None)
        print(f"   {nm}: {len(sold)} of {len(sel)} sold early | seconds left at sale median {sl[len(sl)//2]:.0f}, quartiles {sl[len(sl)//4]:.0f}/{sl[3*len(sl)//4]:.0f} | exit less ask avg {sum(r['exit_less_ask'] for r in sold)/len(sold):+.3f}")
print("DONE")
