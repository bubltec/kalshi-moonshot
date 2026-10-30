"""What does the last-30-seconds volume look like on the cheap sides that WIN against the ones that lose, market by market? Replayed from the tape.

At each checkpoint T (seconds before the close) the CHEAP side is a side whose ask is 2-20c (once per side per market per checkpoint). Over the LOOK seconds before T it measures:
    dollars   taker dollars into that side (contracts x price paid, from flow_1s)
    share     that side's share of all taker dollars in the window (a burst into the cheap side, not the whole market trading)
    drift     the ask now minus the ask LOOK seconds ago (negative: still falling in, positive: already bouncing)
    ratio     the window's dollars against the market's own normal (median second of the 120 s before, scaled)
Then, per market and for all markets together: how many cheap sides, how many won, the average ask and the edge of buying every one (taker fee included, filled LAT s later), and the same by bucket of each
measure, so a pattern shows up as a bucket whose win rate beats its price. Winners are listed. An edge only counts when both the older and newer half of the days agree.

    scripts/cloud.sh run scripts/moonshot_winners_study.py [DAYS=7] [LOOK=10] [LAT=2]
"""
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
LOOK = int(os.environ.get("LOOK", "10"))
LAT = int(os.environ.get("LAT", "2"))
CHECKPOINTS = (20, 12)
CHEAP_LO, CHEAP_HI = 0.02, 0.20
WANT = {"BTC", "ETH", "SOL", "XRP", "DOGE", "WTI", "BRENT", "NATGAS", "GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"}
GROUP = {"BTC": "coins", "ETH": "coins", "SOL": "coins", "XRP": "coins", "DOGE": "coins", "WTI": "energy", "BRENT": "energy", "NATGAS": "energy"}


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else ""


since = time.time() - DAYS * 86400
wrows = [w for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall() if name_of(w["series"]) in WANT]
cases = []  # dict per cheap side
for w in wrows:
    close = float(w["close_ts"])
    quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 60, close)).fetchall()]
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 200), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw
        d[1] += float(nv or 0) * (1.0 - vw)
    if len(quotes) < 5 or not flow:
        continue
    nm = name_of(w["series"])

    def ask_at(t, side):
        q = [x for x in quotes if x[0] <= t and t - x[0] <= 8]
        if not q:
            return None
        return q[-1][2] if side == "yes" else 1.0 - q[-1][1]

    for T in CHECKPOINTS:
        t = int(close - T)
        for side, idx in (("yes", 0), ("no", 1)):
            ask_now = ask_at(t, side)
            if ask_now is None or not CHEAP_LO <= ask_now <= CHEAP_HI:
                continue
            fill = ask_at(t + LAT, side)
            if fill is None or fill > 0.30:
                continue
            ask_then = ask_at(t - LOOK, side)
            dollars = sum(flow.get(t - k, [0.0, 0.0])[idx] for k in range(LOOK))
            total = sum(sum(flow.get(t - k, [0.0, 0.0])) for k in range(LOOK))
            normal = max(statistics.median([sum(flow.get(t - LOOK - k, [0.0, 0.0])) for k in range(120)]) * LOOK, 1.0)
            cases.append({"name": nm, "T": T, "close": close, "ticker": w["ticker"], "side": side, "fill": fill, "won": w["result"] == side,
                          "dollars": dollars, "share": dollars / total if total > 0 else 0.0, "drift": (ask_now - ask_then) if ask_then is not None else 0.0, "ratio": (dollars + 0.0) / normal})

mid = sorted(c["close"] for c in cases)[len(cases) // 2] if cases else 0
for c in cases:
    c["half"] = "old" if c["close"] < mid else "new"


def pnl(c):
    return (100.0 if c["won"] else 0.0) - c["fill"] * 100 - fee_per_contract(c["fill"]) * 100


def line(label, g):
    if not g:
        return
    n = len(g)
    wins = sum(1 for c in g if c["won"])
    ask = sum(c["fill"] for c in g) / n * 100
    e = sum(pnl(c) for c in g) / n
    old = [c for c in g if c["half"] == "old"]
    new = [c for c in g if c["half"] == "new"]
    oe = f"{sum(pnl(c) for c in old) / len(old):>+7.1f}c" if len(old) >= 4 else "      -"
    ne = f"{sum(pnl(c) for c in new) / len(new):>+7.1f}c" if len(new) >= 4 else "      -"
    flag = "  <==" if n >= 12 and e > 0 and len(old) >= 4 and len(new) >= 4 and all(sum(pnl(c) for c in x) > 0 for x in (old, new)) else ""
    print(f"  {label:<26} n {n:>4}  won {wins:>3} ({wins / n:>5.1%})  ask {ask:>5.1f}c  edge {e:>+7.2f}c ({e / ask:>+5.0%})  old {oe}  new {ne}{flag}")


print(f"{len(wrows)} markets, {len(cases)} cheap-side cases ({CHEAP_LO:.2f}-{CHEAP_HI:.2f}) at {CHECKPOINTS} s out, flow over the last {LOOK} s, filled {LAT} s later\n")
for T in CHECKPOINTS:
    print(f"== {T} s before the close, by market")
    for nm in sorted({c["name"] for c in cases}):
        line(nm, [c for c in cases if c["T"] == T and c["name"] == nm])
    line("ALL", [c for c in cases if c["T"] == T])
    for fam in ("coins", "energy"):
        line(f"{fam.upper()}", [c for c in cases if c["T"] == T and GROUP.get(c["name"]) == fam])
    line("METALS", [c for c in cases if c["T"] == T and c["name"] not in GROUP])
    print()
for T in CHECKPOINTS:
    print(f"== {T} s, all markets, by measure")
    base = [c for c in cases if c["T"] == T]
    for lo, hi in ((0, 50), (50, 200), (200, 500), (500, 1e9)):
        line(f"dollars ${lo}-{hi if hi < 1e9 else 'up'}", [c for c in base if lo <= c["dollars"] < hi])
    for lo, hi in ((0, 0.5), (0.5, 0.8), (0.8, 1.01)):
        line(f"share {lo:.0%}-{min(hi, 1):.0%}", [c for c in base if lo <= c["share"] < hi and c["dollars"] >= 20])
    for lo, hi, label in ((-1, -0.03, "ask falling >3c"), (-0.03, 0.03, "ask flat"), (0.03, 1, "ask rising >3c")):
        line(label, [c for c in base if lo <= c["drift"] < hi])
    for lo, hi in ((0, 3), (3, 8), (8, 1e9)):
        line(f"ratio {lo}-{hi if hi < 1e9 else 'up'}x", [c for c in base if lo <= c["ratio"] < hi])
    print()
print("== the cheap sides that won (12 and 20 s out merged, one per market)")
seen = set()
for c in sorted([c for c in cases if c["won"]], key=lambda c: c["close"]):
    if c["ticker"] in seen:
        continue
    seen.add(c["ticker"])
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(c['close']))}  {c['name']:<9} {c['side']:<3} paid {c['fill']:.2f}  ${c['dollars']:>6,.0f} in {LOOK}s  share {c['share']:>4.0%}  drift {c['drift']:>+5.2f}  {c['ratio']:>5.1f}x normal")
