"""Does buying a side at the extreme of its hour wave lose? (2026-11-19, user, on KXGOLD15M-26NOV190830 NO and KXSILVER15M-26NOV191045 NO, each bought after the spot had slid for minutes to the low of its hour, then reversed: "always buying at the dip in the middle of the market. we need to stop this"; "its like the bot has no context of what it is buying")
One question on the same bets (CLAUDE.md rule 4): every live commodity bet (mode trade, action bet, a lane starting commodity, not the hand-placed ones) with a settled result since SINCE (default 2026-11-11 00:00 ET, where the Kalshi perp tape on the host begins) and an hour of the series' own Kalshi perp reference ticks (tape index_ticks, index KP_<series>: the market's own reference, not an outside feed). Position of the spot in the last hour's range, read at the buy and MIRRORED for the bet's side: fav = (spot - low) / (high - low) for a YES bet, 1 - that for a NO bet, so 1.0 means the spot sits at the extreme that favours the bet (a NO bought with the spot at its hour low) and 0.0 at the opposite extreme. The same on the last 15 minutes. Hours whose range is under WIDTH typical minutes (default 3; the typical minute is the rms one-second move of the reference over the hour times sqrt(60)) have no wave to be at the extreme of and are reported as flat. Cuts: under 0.3, 0.3 to 0.7, 0.7 to 0.9, 0.9 or more (a study's cuts, said so). Per cut: bets, average ask, win rate against it (held), net per $100 staked held with the buy's fee and AS TRADED (the bet's own exit, fee on the sale), bets ended under 0.10, and each half of the period. Settings chosen after seeing the results are said to be so.
Run on the host: scripts/cloud.sh run scripts/commodity_wave_position.py [SINCE=<epoch>] [WIDTH=3]"""
import datetime
import math
import os

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 11, tzinfo=ET).timestamp()))
width_min = float(os.environ.get("WIDTH", "3"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall()
bets, nodata, flat = [], 0, 0
for b in rows:
    if not b["ask"]:
        continue
    t0 = float(b["ts"])
    ix = "KP_" + b["ticker"].split("-")[0].removeprefix("KX").removesuffix("15M")
    q = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<=? AND value>0 ORDER BY ts", (ix, t0 - 3600.0, t0)).fetchall()
    if len(q) < 1200:
        nodata += 1
        continue
    ts = [float(x[0]) for x in q]; v = [float(x[1]) for x in q]
    grid, j = [], 0
    for sec in range(int(t0) - 3600, int(t0)):
        while j + 1 < len(ts) and ts[j + 1] <= sec:
            j += 1
        grid.append(v[j] if ts[j] <= sec else None)
    g = [x for x in grid if x is not None]
    dd = [g[i + 1] - g[i] for i in range(len(g) - 1)]
    unit = math.sqrt(sum(x * x for x in dd) / max(len(dd), 1)) * math.sqrt(60.0)
    if unit <= 0:
        nodata += 1
        continue
    hi, lo, now_v = max(g), min(g), g[-1]
    h15 = g[-900:]
    hi15, lo15 = max(h15), min(h15)
    ask = float(b["ask"])
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None
    held = (1.0 if won else 0.0) - ask - fee(ask)
    traded = ((float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)) - ask - fee(ask)
    end = (float(b["exit_price"]) if sold else (1.0 if won else 0.0))
    def fav(lo_, hi_):
        if hi_ - lo_ <= 0:
            return None
        pos = (now_v - lo_) / (hi_ - lo_)
        return pos if b["side"] == "yes" else 1.0 - pos
    r = {"t": t0, "ask": ask, "won": won, "held": held, "traded": traded, "zero": end < 0.10, "fav60": fav(lo, hi), "fav15": fav(lo15, hi15), "wide": (hi - lo) / unit >= width_min, "wide15": (hi15 - lo15) / unit >= width_min}
    bets.append(r)
bets.sort(key=lambda r: r["t"])
n = len(bets)
half = n // 2
first = set(id(r) for r in bets[:half])
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets with an hour of Kalshi perp ticks ({nodata} left out). fav = where the spot sits in its range, 1.0 = at the extreme that favours the bet.")


def show(label, sel):
    if len(sel) < 15:
        print(f"  {label:34s} bets {len(sel):4d}")
        return
    stake = sum(r["ask"] for r in sel)
    h1 = [r for r in sel if id(r) in first]
    h2 = [r for r in sel if id(r) not in first]
    f = lambda xs, k: 100.0 * sum(r[k] for r in xs) / max(sum(r["ask"] for r in xs), 1e-9) if xs else float("nan")
    print(f"  {label:34s} bets {len(sel):4d} | avg ask {100.0 * stake / len(sel):5.1f}c win {100.0 * sum(1 for r in sel if r['won']) / len(sel):5.1f}% | held {sum(r['held'] for r in sel):+7.2f} ({f(sel, 'held'):+5.1f} per $100) | as traded {sum(r['traded'] for r in sel):+7.2f} ({f(sel, 'traded'):+5.1f} per $100) | under 0.10: {sum(1 for r in sel if r['zero']):3d} | per $100 held, first half {f(h1, 'held'):+5.1f} (n {len(h1)}), second {f(h2, 'held'):+5.1f} (n {len(h2)})")


show("all", bets)
for key, wide, name in (("fav60", "wide", "last hour"), ("fav15", "wide15", "last 15 minutes")):
    print(f"--- position in the {name}'s range (hour/15 min under {width_min:g} typical minutes wide = flat)")
    show("flat range", [r for r in bets if not r[wide]])
    w = [r for r in bets if r[wide] and r[key] is not None]
    show("under 0.3 (spot at the far side)", [r for r in w if r[key] < 0.3])
    show("0.3 to 0.7", [r for r in w if 0.3 <= r[key] < 0.7])
    show("0.7 to 0.9", [r for r in w if 0.7 <= r[key] < 0.9])
    show("0.9 or more (the bet's own extreme)", [r for r in w if r[key] >= 0.9])
print("DONE")
