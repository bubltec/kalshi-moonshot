"""Does a tighter sale beat what the live commodity bets actually did, and does that depend on how volatile the market was at the buy? (2026-11-19, user, on KXPLATINUM15M-26NOV190200 NO bought 0.90 and sold 0.25 after a late spike, and KXCOPPER15M-26NOV190230: "i dont think high volatility means we don't buy, but i think it means we tighten our selling")
One question on the same bets (CLAUDE.md rule 4): every live commodity bet (mode trade, action bet, a lane starting commodity, not the hand-placed ones) with a settled result since SINCE (default 2026-11-09 00:00 ET) and tape quotes. What actually happened = the bet's own exit (sold at exit_price, fee on the sale) or settlement. The buy and its fee are the same in both, so only the exit leg differs.
The rule, stated before the run: "a tight sale": after the buy, sell at the bid the first second the side's bid is K typical minutes under its high since the buy, whether or not the bet is ahead (the lane's trail needs the bet ahead and stands aside otherwise). The typical minute is the market's own at the buy (rms one-second mid move so far times sqrt(60)). K tried: 1, 1.5, 2, 3. A sale by the bot's own exit that came earlier than the rule's stands (the rule only adds a sale before it, or before settlement). Taker fee on the rule's sale.
Volatility at the buy = the bot's own `vol Nx` tag in the buy's log line (the market's volatility against its normal), cut in thirds of the bets (a study's cut). Reported per third and per K: bets, bets the rule sold, rule less actual in dollars per contract summed, per $100 staked, bets it beat and lost to what happened, and the bets it sold that went on to win (the cost of selling a dip). Settings chosen after seeing the results are said to be so.
Run on the host: scripts/cloud.sh run scripts/commodity_tight_sell_by_vol.py [SINCE=<epoch>]"""
import datetime
import math
import os
import re

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
KS = (1.0, 1.5, 2.0, 3.0)
ACTIVE = float(os.environ.get("ACTIVE_LEFT", "0"))  # 0 = the rule is on from the buy; else it only sells with this many seconds left or fewer (a sixth of the 15-minute window is 150)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.lane, d.exit_price, d.exit_ts, d.reason, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall()
bets, nodata = [], 0
for b in rows:
    m = re.search(r"\[vol ([0-9.]+)x\]", str(b["reason"] or ""))
    if not m or not b["ask"]:
        continue
    t0, close = float(b["ts"]), float(b["close_ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], close)).fetchall()
    if len(q) < 120:
        nodata += 1
        continue
    ts = [float(x[0]) for x in q]; yb = [float(x[1]) for x in q]; ya = [float(x[2]) for x in q]
    side_bid = yb if b["side"] == "yes" else [1.0 - a for a in ya]
    mid = [(yb[i] + ya[i]) / 2.0 for i in range(len(ts))]
    # typical minute so far at the buy: per-second mids from the first quote to the buy
    secs, j, ms = list(range(int(ts[0]), int(t0))), 0, []
    for sec in secs:
        while j + 1 < len(ts) and ts[j + 1] <= sec:
            j += 1
        ms.append(mid[j])
    dd = [ms[i + 1] - ms[i] for i in range(len(ms) - 1)]
    if len(dd) < 120:
        nodata += 1
        continue
    unit = math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0)
    if unit <= 0:
        continue
    won = b["result"] == b["side"]
    ask = float(b["ask"])
    sold = b["exit_price"] is not None
    actual = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)
    t_exit = float(b["exit_ts"]) if sold and b["exit_ts"] else close
    path = [(ts[i], side_bid[i]) for i in range(len(ts)) if t0 < ts[i] < t_exit]
    if not path:
        nodata += 1
        continue
    late = [bb for tt, bb in path if close - tt <= 150]
    r = {"vol": float(m.group(1)), "ask": ask, "won": won, "actual": actual, "lost_late": (not won) and bool(late) and late[0] >= ask - 0.05}
    for k in KS:
        peak = max(side_bid[max(0, max(i for i in range(len(ts)) if ts[i] <= t0))] if any(x <= t0 for x in ts) else ask, 0.0)
        hit = None
        for tt, bb in path:
            peak = max(peak, bb)
            if bb <= peak - k * unit and (ACTIVE <= 0 or close - tt <= ACTIVE):
                hit = bb
                break
        r[f"rule{k}"] = (hit - fee(hit)) if hit is not None else actual
        r[f"sold{k}"] = hit is not None
    bets.append(r)
bets.sort(key=lambda r: r["vol"])
n = len(bets)
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets with the vol tag and tape quotes ({nodata} left out for no quotes). Rule less actual per contract; 'sold' = the rule's sale came before what really happened; 'sold bets that won' = sales of bets that went on to win.")
vols = [r["vol"] for r in bets]
print(f"vol tag: low {vols[0]:.2f}, thirds at {vols[n // 3]:.2f} and {vols[2 * n // 3]:.2f}, high {vols[-1]:.2f}")
print(f"lost bets whose side bid was still within 5c of the buy price with 150 s (a sixth of the window) left: {sum(1 for r in bets if r['lost_late'])} of {sum(1 for r in bets if not r['won'])} lost")
print(f"rule active from: {'the buy' if ACTIVE <= 0 else str(ACTIVE) + ' s left or fewer'}")
print(f"actual: net of the exit leg per contract {sum(r['actual'] for r in bets) / n:.3f}; lost bets {sum(1 for r in bets if not r['won'])}")
for name, sel in (("low vol third", bets[:n // 3]), ("middle third", bets[n // 3:2 * n // 3]), ("high vol third", bets[2 * n // 3:]), ("all", bets)):
    print(f"--- {name}: {len(sel)} bets, avg ask {100.0 * sum(r['ask'] for r in sel) / len(sel):.1f}c, won {100.0 * sum(1 for r in sel if r['won']) / len(sel):.1f}%")
    stake = sum(r["ask"] for r in sel)
    for k in KS:
        d = [r[f"rule{k}"] - r["actual"] for r in sel]
        sold = [r for r in sel if r[f"sold{k}"]]
        print(f"   K {k:g}: sold {len(sold):4d} | rule less actual {sum(d):+8.2f} ({100.0 * sum(d) / stake:+5.1f} per $100) | beat {sum(1 for x in d if x > 1e-9):4d} lost {sum(1 for x in d if x < -1e-9):4d} | sold bets that won {sum(1 for r in sold if r['won']):4d} of {len(sold)}")
print("--- the size of the losses (per contract, against the buy price; a bet 'lost big' ends 0.5 or more under what it cost)")
for k in KS:
    a = [r["actual"] - r["ask"] for r in bets]
    b = [r[f"rule{k}"] - r["ask"] for r in bets]
    big_a, big_b = sum(1 for x in a if x <= -0.5), sum(1 for x in b if x <= -0.5)
    zero_a, zero_b = sum(1 for r in bets if r["actual"] < 0.10), sum(1 for r in bets if r[f"rule{k}"] < 0.10)
    loss_a, loss_b = sum(x for x in a if x < 0), sum(x for x in b if x < 0)
    gain_a, gain_b = sum(x for x in a if x > 0), sum(x for x in b if x > 0)
    print(f"   K {k:g}: lost big {big_a} -> {big_b} | ends under 0.10: {zero_a} -> {zero_b} | all losses {loss_a:+.2f} -> {loss_b:+.2f} | all gains {gain_a:+.2f} -> {gain_b:+.2f} | worst bet {min(a):+.2f} -> {min(b):+.2f}")
print("DONE")
