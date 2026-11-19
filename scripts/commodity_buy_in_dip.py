"""Does buying a side while its bid is in a dip lose? (2026-11-19, user, on KXGOLD15M-26NOV190830 NO bought 0.90 one second after its bid stepped 0.91 to 0.85, then sold 0.35 as the market finished Up: "i think the problem is we bought right before it shifted"; "don't buy NO in a dip...")
One question on the same bets (CLAUDE.md rule 4): every live commodity bet (mode trade, action bet, a lane starting commodity, not the hand-placed ones) with a settled result and tape quotes since SINCE (default 2026-11-09 00:00 ET). The dip is read at the buy from Kalshi's own quotes, on the bet's OWN side, mirrored for YES and NO (no side claims): the side's bid under its highest bid of the 90 s before the buy, in the market's own typical minutes (rms one-second mid move so far times sqrt(60); nothing in cents, nothing keyed on a market). Cut: no dip (0), under 1 typical minute, 1 to 2, over 2 (a study's cuts, said so), and the fifths of the dip. Per cut: bets, the average ask, the win rate against it (held to settlement), net per $100 staked with the buy's fee held to settlement and AS TRADED (the bet's own exit, fee on the sale), bets lost to zero (ending under 0.10), and the cut in each half of the period. Settings chosen after seeing the results are said to be so.
Run on the host: scripts/cloud.sh run scripts/commodity_buy_in_dip.py [SINCE=<epoch>]"""
import datetime
import math
import os

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall()
bets, nodata = [], 0
for b in rows:
    if not b["ask"]:
        continue
    t0, close = float(b["ts"]), float(b["close_ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], t0)).fetchall()
    if len(q) < 120:
        nodata += 1
        continue
    ts = [float(x[0]) for x in q]; yb = [float(x[1]) for x in q]; ya = [float(x[2]) for x in q]
    side_bid = yb if b["side"] == "yes" else [1.0 - a for a in ya]
    mid = [(yb[i] + ya[i]) / 2.0 for i in range(len(ts))]
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
    recent = [side_bid[i] for i in range(len(ts)) if ts[i] >= t0 - 90.0]
    if not recent:
        nodata += 1
        continue
    dip = max(0.0, (max(recent) - side_bid[-1]) / unit)
    ask = float(b["ask"])
    won = b["result"] == b["side"]
    held = (1.0 if won else 0.0) - ask - fee(ask)
    sold = b["exit_price"] is not None
    traded = ((float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)) - ask - fee(ask)
    bets.append({"t": t0, "dip": dip, "ask": ask, "won": won, "held": held, "traded": traded, "zero": (float(b["exit_price"]) if sold else (1.0 if won else 0.0)) < 0.10})
bets.sort(key=lambda r: r["t"])
n = len(bets)
half = n // 2
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets with the dip read ({nodata} left out for no quotes). Dip = the side's bid under its 90 s high at the buy, in the market's own typical minutes.")


def show(label, sel):
    if len(sel) < 20:
        print(f"  {label:30s} bets {len(sel):4d}")
        return
    stake = sum(r["ask"] for r in sel)
    h1 = [r for r in sel if r in bets[:half]]
    h2 = [r for r in sel if r not in bets[:half]]
    f = lambda xs, k: 100.0 * sum(r[k] for r in xs) / max(sum(r["ask"] for r in xs), 1e-9) if xs else float("nan")
    print(f"  {label:30s} bets {len(sel):4d} | avg ask {100.0 * stake / len(sel):5.1f}c win {100.0 * sum(1 for r in sel if r['won']) / len(sel):5.1f}% | held {sum(r['held'] for r in sel):+7.2f} ({f(sel, 'held'):+5.1f} per $100) | as traded {sum(r['traded'] for r in sel):+7.2f} ({f(sel, 'traded'):+5.1f} per $100) | ended under 0.10: {sum(1 for r in sel if r['zero']):3d} | per $100 held, first half {f(h1, 'held'):+5.1f}, second {f(h2, 'held'):+5.1f}")


show("all", bets)
print("--- by the dip (cuts chosen after seeing the readings)")
show("no dip (0)", [r for r in bets if r["dip"] <= 1e-9])
show("under 1 typical minute", [r for r in bets if 1e-9 < r["dip"] < 1.0])
show("1 to 2 typical minutes", [r for r in bets if 1.0 <= r["dip"] < 2.0])
show("2 or more typical minutes", [r for r in bets if r["dip"] >= 2.0])
print("--- fifths of the dip")
srt = sorted(bets, key=lambda r: r["dip"])
for i in range(5):
    sel = srt[i * n // 5:(i + 1) * n // 5]
    show(f"fifth {i + 1} ({sel[0]['dip']:.2f} to {sel[-1]['dip']:.2f})", sel)
print("DONE")
