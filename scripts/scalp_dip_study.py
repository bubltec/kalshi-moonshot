"""HOW FAR SHOULD A DIP FALL TO SELL AT ONCE: the scalp exit sells when the bid is scalp_dip (3c) under the high it has followed since it climbed scalp_up (10c) above the price paid. A dip that has to hold scalp_dip_seconds (3 s) before it sells avoids selling a
wobble, but in a sharp reversal the bid keeps falling while it waits. A hard drop lets a fall of H or more under the high sell at once.

Every early_scalp bet on the tape (paper and live), replayed second by second from the bet through to 10 s before the close, under each rule: no wait (the old rule), the 3 s hold, and the 3 s hold with a hard drop of 5c to 15c. A sale is priced at the bid ONE SECOND after
the rule fires (an order takes a moment), with the sale fee paid.
Reported: how many bets each rule sells, the average sale on the bets every rule sells (a like-for-like comparison, higher is better), and for the bets the 3 s hold would NOT have sold but another rule does (a dip that recovers), what the sale got against holding to the result.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/scalp_dip_study.py
"""
import bisect
import math
import statistics
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
UP, DIP = 0.10, 0.03  # the lane's scalp_up and scalp_dip
VARIANTS = [("no wait (old rule)", 0.0, None), ("3 s hold", 3.0, None)] + [(f"3 s hold + hard drop {h:.2f}", 3.0, h) for h in (0.05, 0.06, 0.08, 0.10, 0.15)]
bets = main.db.execute(
    """SELECT d.ticker, d.side, d.ask, d.ts, d.close_time, w.result FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker
       WHERE d.lane = 'early_scalp' AND d.action = 'bet' AND d.ask > 0 AND d.close_time IS NOT NULL ORDER BY d.ts""").fetchall()


def bids_for(ticker, side, t0, close):
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (ticker, t0 - 5, close)).fetchall()
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(t0), int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            out[sec] = float(q[i][1]) if side == "yes" else 1.0 - float(q[i][2])
    return out


def run(bids, entry, t0, close, hold, hard):
    """The second the rule fires, or None."""
    arm, high, since = entry + UP, 0.0, None
    for sec in range(int(t0) + 1, int(close) - 9):
        b = bids.get(sec)
        if b is None:
            continue
        if b >= arm - 1e-9:
            high = max(high, b)
        if high < arm - 1e-9 or b > high - DIP + 1e-9:
            since = None
            continue
        if hold <= 0 or (hard is not None and high - b >= hard - 1e-9):
            return sec
        since = sec if since is None else since
        if sec - since >= hold:
            return sec
    return None


fires = {name: {} for name, _, _ in VARIANTS}
n = 0
for r in bets:
    close = datetime.fromisoformat(str(r["close_time"])).timestamp()
    bids = bids_for(r["ticker"], r["side"], float(r["ts"]), close)
    if len(bids) < 30:
        continue
    n += 1
    for name, hold, hard in VARIANTS:
        sec = run(bids, float(r["ask"]), float(r["ts"]), close, hold, hard)
        if sec is not None:
            after = bids.get(sec + 1, bids.get(sec))
            fires[name][r["ticker"] + str(r["ts"])] = (max(after, 0.0) - (fee(after) if after > 0 else 0.0), float(r["ask"]), 1.0 if r["result"] == r["side"] else (0.0 if r["result"] else None))
print(f"{n} early_scalp bets replayed (climb of {UP:.2f} arms the rule; a dip of {DIP:.2f} sells).\n")
base = set(fires["3 s hold"])
print(f"{'rule':32} {'sells':>6} {'avg sale':>9} {'vs the 3 s hold, same bets (+- standard error)':>50}")
for name, _, _ in VARIANTS:
    keys = [k for k in base if k in fires[name]]
    common = [fires[name][k][0] for k in keys]
    paired = [fires[name][k][0] - fires["3 s hold"][k][0] for k in keys]
    se = statistics.pstdev(paired) / math.sqrt(len(paired)) if len(paired) > 1 else float("nan")
    print(f"{name:32} {len(fires[name]):>6} {statistics.mean(common):>9.3f} {statistics.mean(paired):>+30.4f} +-{se:.4f}" if common else f"{name:32} {len(fires[name]):>6}")
print("\nSales a rule makes that the 3 s hold would NOT (the dip recovered inside 3 s, or never held): sale value against holding to the result (settled bets only)")
print(f"{'rule':32} {'extra sells':>11} {'avg sale':>9} {'avg hold payoff':>16} {'sale better by':>15} {'+-':>6}")
for name, _, _ in VARIANTS:
    if name == "3 s hold":
        continue
    extra = [v for k, v in fires[name].items() if k not in base and v[2] is not None]
    if len(extra) < 3:
        print(f"{name:32} {len(extra):>11}")
        continue
    diff = [v[0] - v[2] for v in extra]
    se = statistics.pstdev(diff) / math.sqrt(len(diff))
    print(f"{name:32} {len(extra):>11} {statistics.mean(v[0] for v in extra):>9.3f} {statistics.mean(v[2] for v in extra):>16.3f} {statistics.mean(diff):>+15.3f} {se:>6.3f}")
