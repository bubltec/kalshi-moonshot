"""Last-minute lottery tickets: sides priced 0-5 cents with under a minute left in a 15-minute window. Replays recorded ticks.

For each settled window with ticks, at 60, 45, 30 and 15 seconds before the close, take each side whose ask is 5 cents or
less and note whether its bid has been trending up over the previous five minutes. Reports how often those tickets won against
what they cost (fees included). Places nothing.
    python scripts/last_minute_study.py moonshot.db tape.db
"""
import bisect
import sqlite3
import sys

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
wins = db.execute("select ticker, series, close_ts, result from windows where result in ('yes','no') and series like '%15M'").fetchall()
cache = {}


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


def at(tk, when):
    ts, rows = quotes(tk)
    i = bisect.bisect_right(ts, when) - 1
    return rows[i] if i >= 0 and when - ts[i] < 120 else None


E = []
for tk, series, close, result in wins:
    ts, _ = quotes(tk)
    if not ts or ts[0] > close - 330:
        continue
    for left in (60, 45, 30, 15):
        now, then = at(tk, close - left), at(tk, close - 300)
        if now is None or then is None:
            continue
        for side in ("yes", "no"):
            ask = now[2] if side == "yes" else 1 - now[1]
            bid_now = now[1] if side == "yes" else 1 - now[2]
            bid_then = then[1] if side == "yes" else 1 - then[2]
            if 0.01 <= ask <= 0.05:
                E.append(dict(series=series, left=left, side=side, ask=ask, trend=bid_now - bid_then, won=result == side, tk=tk, close=close))


def roi(g):
    cost = sum(e["ask"] + fee(e["ask"]) for e in g)
    return sum(e["won"] for e in g) / cost - 1 if cost else float("nan")


def show(label, g):
    if len(g) < 10:
        return
    pay = sum(1 / e["ask"] for e in g if e["won"]) / max(1, sum(e["won"] for e in g))
    print(f"{label:40} n={len(g):5} won {sum(e['won'] for e in g):3} ({sum(e['won'] for e in g) / len(g):5.1%}) "
          f"avg ask {sum(e['ask'] for e in g) / len(g):.3f} avg payout {pay:5.1f}x ROI {roi(g):+7.1%}")


print(f"{len(E)} ticket-moments at 1-5 cents in {len({e['tk'] for e in E})} windows\n")
show("all", E)
for left in (60, 45, 30, 15):
    show(f"  {left}s left", [e for e in E if e["left"] == left])
print("\nBy the side's bid trend over the previous 5 minutes (at 60s left, 1-5c):")
base = [e for e in E if e["left"] == 60]
for name, fn in (("falling", lambda t: t < -0.005), ("flat", lambda t: -0.005 <= t <= 0.005), ("rising up to 2c", lambda t: 0.005 < t <= 0.02),
                 ("rising more than 2c", lambda t: t > 0.02)):
    show(f"  {name}", [e for e in base if fn(e["trend"])])
print("\nBy price (60s left, any trend):")
for lo, hi in ((0.01, 0.015), (0.015, 0.025), (0.025, 0.05)):
    show(f"  {lo:.3f}-{hi:.3f}", [e for e in base if lo <= e["ask"] < hi or (hi == 0.05 and e["ask"] == 0.05)])
print("\nWinners (60s left): payout multiples:")
w = sorted((1 / e["ask"] for e in base if e["won"]), reverse=True)
print("  ", ", ".join(f"{x:.0f}x" for x in w[:15]), f"... of {len(w)}")
