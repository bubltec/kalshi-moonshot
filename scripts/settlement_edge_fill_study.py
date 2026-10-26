"""FILL REALISM of the settlement_edge paper record: could the logged price actually have been bought?

The lane (settlement_edge.py) logs a paper bet at the socket ask the second its exact settlement chance beats
that ask by 5/10/15 points, in the last 150-20 seconds of a 15-minute crypto market. A paper record at that
speed flatters itself three ways: the quote can be gone by the time an order lands, the ask can have moved
toward the model (the chance was computed from the same tape the market makers watch), and the size at that
ask can be a handful of contracts. This replays every logged bet against the tape's quotes: at delays of
0/1/2/5 seconds after the logged second, what did the same side's ask cost, and what would buying then have
returned, held to settlement, fee paid? A delayed buy counts as available only if the ask is still within
TOLERANCE of the logged ask; the return is reported both for the buys that survive that test and as a
per-logged-bet figure where a missed fill earns nothing. Standard errors are clustered by 15-minute slot.

Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/settlement_edge_fill_study.py
"""
import bisect
import collections
import json
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DELAYS = (0.0, 1.0, 2.0, 5.0)
TOLERANCE = 0.01  # the delayed ask may be at most this far over the logged ask and still count as the same fill


def side_ask(bid, ask, side):
    return ask if side == "yes" else 1.0 - bid


rows = main.db.execute(
    "SELECT ts, ticker, side, ask, model_p, outcome, context FROM decisions "
    "WHERE lane='settlement_edge' AND action='bet' AND outcome IN ('yes','no') ORDER BY ts"
).fetchall()

bets = []
quotes: dict[str, tuple[list[float], list[tuple[float, float]]]] = {}
for r in rows:
    tk = r["ticker"]
    if tk not in quotes:
        q = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)
        ).fetchall()
        quotes[tk] = ([x[0] for x in q], [(x[1], x[2]) for x in q])
    qts, qba = quotes[tk]
    if not qts:
        continue
    try:
        bar = float(json.loads(r["context"] or "{}").get("bar", 0.0))
    except ValueError:
        bar = 0.0
    win = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    delayed = {}
    for d in DELAYS:
        i = bisect.bisect_right(qts, r["ts"] + d) - 1
        if i < 0 or qts[i] < r["ts"] + d - 5.0:
            delayed[d] = None
        else:
            delayed[d] = side_ask(qba[i][0], qba[i][1], r["side"] or "yes")
    bets.append({
        "ts": r["ts"], "slot": tk.split("-")[1] if "-" in tk else tk, "series": tk.split("-")[0],
        "ask": float(r["ask"]), "p": float(r["model_p"] or 0.0), "win": win, "bar": bar, "delayed": delayed,
    })


def ret(price, win):
    c = price + fee(price)
    return (win - c) / c


def line(label, group, delay):
    if not group:
        return f"  {label:34} no bets"
    filled = [b for b in group if b["delayed"][delay] is not None and b["delayed"][delay] <= b["ask"] + TOLERANCE]
    if not filled:
        return f"  {label:34} {len(group):4} logged, none still buyable at +{delay:g}s"
    per_slot = collections.defaultdict(list)
    for b in filled:
        per_slot[b["slot"]].append(ret(b["delayed"][delay], b["win"]))
    means = [sum(v) / len(v) for v in per_slot.values()]
    se = (100 * math.sqrt(sum((x - statistics.mean(means)) ** 2 for x in means) / (len(means) - 1) / len(means))
          if len(means) > 2 else float("nan"))
    per_logged = sum(ret(b["delayed"][delay], b["win"]) for b in filled) / len(group)
    drift = statistics.mean(b["delayed"][delay] - b["ask"] for b in filled)
    return (f"  {label:34} {len(group):4} logged, {len(filled):4} buyable ({100 * len(filled) / len(group):3.0f}%), "
            f"ask drift {100 * drift:+5.1f}c, return per fill {100 * statistics.mean(ret(b['delayed'][delay], b['win']) for b in filled):+6.1f}% "
            f"+-{se:.0f} (by slot), per logged bet {100 * per_logged:+6.1f}%")


print(f"{len(bets)} settled settlement_edge paper bets with tape quotes")
print(f"a delayed buy counts when its ask is within {100 * TOLERANCE:.0f}c of the logged ask; returns hold to settlement, buy fee paid")
for d in DELAYS:
    print(line(f"all bets, filled at +{d:g}s", bets, d))
print("by the edge bar it fired at (filled at +2s):")
for bar in (0.05, 0.10, 0.15):
    print(line(f"bar {100 * bar:.0f} points", [b for b in bets if abs(b["bar"] - bar) < 1e-9], 2.0))
print("by series (filled at +2s):")
for series in sorted({b["series"] for b in bets}):
    print(line(series, [b for b in bets if b["series"] == series], 2.0))
half = len(bets) // 2
print("by time (filled at +2s):")
print(line("earlier half", bets[:half], 2.0))
print(line("later half", bets[half:], 2.0))
