"""Does order flow tell us anything the price does not? Uses the tape's trade flow (flow_1s) and top-of-book sizes.

For every settled 15-minute window with flow recorded, at minutes 4, 6, 8 and 10 take each side priced 55-85 cents and measure
  flow   taker volume over the previous `span` seconds: (contracts bought on that side - on the other side) / total
  depth  top-of-book size at that side's bid against the other side's, from the last quote
then ask whether the side won more (or less) often than its price implies, with the later 40% of windows held out.
Also: how often the bid is higher 60 seconds later when flow was strongly on that side. Public data; places nothing.
    python scripts/flow_study.py moonshot.db tape.db [span_seconds]
"""
import bisect
import sqlite3
import sys

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
SPAN = int(sys.argv[3]) if len(sys.argv) > 3 else 120
MIN_VOL = 30.0  # contracts in the span, below which a flow reading is noise
wins = db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
first_flow = tape.execute("select min(ts) from flow_1s").fetchone()[0]
if first_flow is None:
    raise SystemExit("no trade flow recorded yet: the recorder was just turned on. Run again after a day.")
qc, fc = {}, {}


def quotes(tk):
    if tk not in qc:
        r = tape.execute("select ts,bid,ask,bid_size,ask_size from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        qc[tk] = ([x[0] for x in r], r)
    return qc[tk]


def flow(tk, a, b):
    if tk not in fc:
        fc[tk] = tape.execute("select ts,yes_vol,no_vol from flow_1s where ticker=? order by ts", (tk,)).fetchall()
    rows = [x for x in fc[tk] if a <= x[0] < b]
    return sum(x[1] for x in rows), sum(x[2] for x in rows)


E = []
for tk, close, res in wins:
    if close - 900 < first_flow:
        continue
    ts, rows = quotes(tk)
    if not ts:
        continue
    for m in (4, 6, 8, 10):
        t0 = close - (15 - m) * 60
        i = bisect.bisect_right(ts, t0) - 1
        j = bisect.bisect_right(ts, t0 + 60) - 1
        if i < 0 or j <= i:
            continue
        yv, nv = flow(tk, t0 - SPAN, t0)
        for side in ("yes", "no"):
            price = rows[i][2] if side == "yes" else 1 - rows[i][1]
            if not 0.55 <= price <= 0.85:
                continue
            mine, other = (yv, nv) if side == "yes" else (nv, yv)
            tot = mine + other
            f = (mine - other) / tot if tot >= MIN_VOL else None
            bid_sz, ask_sz = (rows[i][3], rows[i][4]) if side == "yes" else (rows[i][4], rows[i][3])
            depth = None if not bid_sz or not ask_sz else (bid_sz - ask_sz) / (bid_sz + ask_sz)
            bid_now = rows[i][1] if side == "yes" else 1 - rows[i][2]
            bid_next = rows[j][1] if side == "yes" else 1 - rows[j][2]
            E.append(dict(close=close, price=price, flow=f, depth=depth, won=res == side, up60=bid_next - bid_now))

print(f"{len(E)} side-moments (55-85c) in {len({e['close'] for e in E})} window-times; flow span {SPAN}s\n")
if len(E) < 200:
    raise SystemExit("too few so far to say anything: run again after a day of recording.")


def roi(g):
    cost = sum(e["price"] + fee(e["price"]) for e in g)
    return sum(e["won"] for e in g) / cost - 1 if cost else float("nan")


def show(label, g):
    if len(g) < 25:
        return
    cut = sorted(e["close"] for e in g)[int(len(g) * 0.6)]
    tr, te = [e for e in g if e["close"] < cut], [e for e in g if e["close"] >= cut]
    p = sum(e["price"] for e in g) / len(g)
    w = sum(e["won"] for e in g) / len(g)
    ups = sum(1 for e in g if e["up60"] > 0) / len(g)
    print(f"  {label:34} n={len(g):5} won {w:5.1%} vs price {p:5.1%} ROI {roi(g):+6.1%} (older {roi(tr):+6.1%}, newer {roi(te):+6.1%}) bid up 60s later {ups:4.0%}")


have = [e for e in E if e["flow"] is not None]
print(f"with enough flow to read (>= {MIN_VOL:.0f} contracts): {len(have)}")
print("\nBy taker flow on the side over the previous", SPAN, "s:")
for name, fn in (("strongly against it (< -40%)", lambda f: f < -0.4), ("against (-40..-10%)", lambda f: -0.4 <= f < -0.1),
                 ("balanced (-10..+10%)", lambda f: -0.1 <= f <= 0.1), ("for it (+10..+40%)", lambda f: 0.1 < f <= 0.4),
                 ("strongly for it (> +40%)", lambda f: f > 0.4)):
    show(name, [e for e in have if fn(e["flow"])])
print("\nBy top-of-book depth on the side's bid vs its ask:")
dep = [e for e in E if e["depth"] is not None]
for name, fn in (("ask side thicker (< -30%)", lambda d: d < -0.3), ("about even", lambda d: -0.3 <= d <= 0.3), ("bid side thicker (> +30%)", lambda d: d > 0.3)):
    show(name, [e for e in dep if fn(e["depth"])])
print("\nBoth: strong flow for it AND a thicker bid:")
show("flow > +25% and bid thicker", [e for e in have if e["flow"] > 0.25 and e["depth"] is not None and e["depth"] > 0.2])
print("\nA flow signal that holds only on the older half or only the newer half is a regime, not an edge.")
