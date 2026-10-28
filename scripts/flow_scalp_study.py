"""HOW BIG ARE THE EARLY_SCALP LOSSES, WHAT CUTS THEM, AND DOES BUY FLOW OR THE BOOK PICK BETTER ENTRIES: every early_scalp-style entry on the tape (today's band 14c-42c, a side not falling over 10 s, first per side per window, bought at the ask one second later, the lane's exits and fees as in early_rise_study.py).
A: how the money is lost: each entry's exit (scalp sale, cash-up, cash-drop, last-seconds hover sale, held to the result) with its mean result and its share of the total loss, the size of a typical loss against the stake, and a LOSS LINE swept on top of the lane (sell when the bid is X under the price paid, priced at the bid one second later):
   mean net, worst result, and the average losing trade, so a stop that shrinks the big losses can be weighed against the wins it cuts off.
B: entry picking by order flow and book: the share of taker dollars in the last 10 s and 30 s that bought THIS side (against the other side), its pace in the last 10 s against the minute before, and the resting book at the quote (this side's bid size against its ask size).
   Reported per group: entries, how often the bid ever got 6c over the price paid (the scalp arms; the bet's result does not matter for a scalp), the average best bid over the price paid,  mean net per contract after fees, per dollar staked and the window-clustered standard error. Read-only.
    docker compose exec -T bot python - < scripts/flow_scalp_study.py   (SINCE_DAYS=7)
"""
import bisect
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
days = float(os.environ.get("SINCE_DAYS", "7"))
UP, DIP, HARD, HOLD, CASH_MIN, CASH_DROP = 0.06, 0.04, 0.09, 3, 12 * 60, 0.30
CAP = 0.50
def grid(ticker, t0, t1):
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, t0 - 20, t1)).fetchall()
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(t0), int(t1) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            out[sec] = (float(q[i][1]), float(q[i][2]))
    return out


def side_px(g, sec, side):
    x = g.get(sec)
    if x is None:
        return None
    return (x[0], x[1]) if side == "yes" else (1.0 - x[1], 1.0 - x[0])  # (bid, ask) in this side's terms


# (loss line, only after this many seconds held, sell if still not armed after this many seconds); the first is the lane as it is
STOPS = [(None, 0, 0), (0.05, 0, 0), (0.10, 0, 0), (0.15, 0, 0), (0.10, 30, 0), (0.10, 60, 0), (0.10, 120, 0), (0.15, 60, 0), (None, 0, 60), (None, 0, 120), (None, 0, 180), (None, 0, 300), (0.15, 0, 120), (0.10, 0, 180)]
BASE = STOPS[0]


def exit_kind(g, side, e, paid, close, won, cfg):
    """(net proceeds per contract after the sale fee, how it ended)."""
    stop, after, give_up = cfg
    high, since = 0.0, None
    t0 = close - 900
    for sec in range(e + 1, int(close) - 4):
        px = side_px(g, sec, side)
        if px is None:
            continue
        b = px[0]
        left, el, age = close - sec, sec - t0, sec - e
        kind = None
        if stop is not None and age >= after and b <= paid - stop:
            kind = "stop"
        elif give_up and age >= give_up and high < paid + UP - 1e-9:
            kind = "time exit"
        elif el >= CASH_MIN and b > paid:
            kind = "cash-up"
        elif b <= paid - CASH_DROP and el >= CASH_MIN:
            kind = "cash-drop"
        elif left <= 5 and b < paid:
            kind = "hover sale"
        if kind is None:
            if b >= paid + UP - 1e-9:
                high = max(high, b)
            if high >= paid + UP - 1e-9:
                if high - b >= HARD - 1e-9:
                    kind = "scalp sale"
                elif b <= high - DIP + 1e-9:
                    since = sec if since is None else since
                    kind = "scalp sale" if sec - since >= HOLD else None
                else:
                    since = None
        if kind:
            after = side_px(g, sec + 1, side) or px
            return max(after[0], 0.0) - (fee(after[0]) if after[0] > 0 else 0.0), kind
    return (1.0 if won else 0.0), ("held, won" if won else "held, lost")


def flow_dollars(fl, sec, side, k):
    tot_side = tot_other = 0.0
    for t in range(sec - k + 1, sec + 1):
        x = fl.get(t)
        if not x or x[2] is None:
            continue
        yes_d, no_d = x[0] * x[2], x[1] * (1.0 - x[2])
        tot_side += yes_d if side == "yes" else no_d
        tot_other += no_d if side == "yes" else yes_d
    return tot_side, tot_other


rows = []  # dicts
wins = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND close_ts > extract(epoch from now()) - ?*86400", (days,)).fetchall()
n = 0
for w in wins:
    close = float(w["close_ts"])
    t0 = close - 900
    g = grid(w["ticker"], t0, close)
    if len(g) < 300:
        continue
    fl = {int(r[0]): (float(r[1]), float(r[2]), None if r[3] is None else float(r[3])) for r in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], t0 - 5, close)).fetchall()}
    sizes = tape.execute("SELECT ts, bid_size, ask_size FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], t0, close)).fetchall()
    sts = [float(r[0]) for r in sizes]
    n += 1
    for side in ("yes", "no"):
        won = w["result"] == side
        for sec in range(int(t0 + 30), int(t0 + 540)):
            cur, prev = side_px(g, sec, side), side_px(g, sec - 10, side)
            if cur is None or prev is None or not (0.14 <= cur[1] <= 0.42 and cur[1] >= prev[1] - 1e-9):
                continue
            fill = side_px(g, sec + 1, side)
            if fill is None or not (0.05 <= fill[1] <= CAP):
                break
            paid = fill[1]
            cost = paid + fee(paid)
            r = {"w": close, "cost": cost, "paid": paid}
            bids_after = [px[0] for px in (side_px(g, k, side) for k in range(sec + 1, int(close) - 4)) if px is not None]
            r["peak"] = (max(bids_after) - paid) if bids_after else 0.0  # the most the bid ever stood above the price paid: the profit that was on offer
            for st in STOPS:
                got, kind = exit_kind(g, side, sec + 1, paid, close, won, st)
                r[st] = (got - cost, kind)
            s10, o10 = flow_dollars(fl, sec, side, 10)
            s30, o30 = flow_dollars(fl, sec, side, 30)
            s60, _ = flow_dollars(fl, sec - 10, side, 60)
            r["share10"] = s10 / (s10 + o10) if s10 + o10 >= 5 else None
            r["share30"] = s30 / (s30 + o30) if s30 + o30 >= 5 else None
            r["dollars10"] = s10
            r["pace"] = (s10 / 10.0) / (s60 / 60.0) if s60 >= 5 else None
            i = bisect.bisect_right(sts, sec) - 1
            if i >= 0 and sec - sts[i] <= 15 and sizes[i][1] is not None and sizes[i][2] is not None:
                bs, as_ = float(sizes[i][1]), float(sizes[i][2])
                yes_bid, yes_ask = bs, as_
                mine, theirs = (yes_bid, yes_ask) if side == "yes" else (yes_ask, yes_bid)  # NO's bid size is the YES ask size
                r["book"] = mine / (mine + theirs) if mine + theirs > 0 else None
            else:
                r["book"] = None
            rows.append(r)
            break
print(f"{n} windows, {days:.0f} days, {len(rows)} entries\n")


def per_dollar(r, st):
    return [x[st][0] / x["cost"] for x in r]


def se_of(r, vals):
    by = defaultdict(float)
    for x, v in zip(r, vals, strict=True):
        by[x["w"]] += v
    return math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r) if len(by) > 1 else float("nan")


print(f"the bid reached +6c over the price paid at some point (the scalp arms) in {100 * sum(x['peak'] >= UP - 1e-9 for x in rows) / len(rows):.0f}% of entries; the average best bid is {100 * statistics.mean(x['peak'] for x in rows):+.1f}c over the price paid\n")
print("A. how the money is lost (the lane as it is)")
print(f"  {'exit':14} {'n':>5} {'mean net/contract':>18} {'share of total loss':>20} {'avg result per $ staked':>24}")
total_loss = sum(-x[BASE][0] for x in rows if x[BASE][0] < 0)
for kind in ("scalp sale", "cash-up", "cash-drop", "hover sale", "held, won", "held, lost"):
    g_ = [x for x in rows if x[BASE][1] == kind]
    if not g_:
        continue
    loss = sum(-x[BASE][0] for x in g_ if x[BASE][0] < 0)
    print(f"  {kind:14} {len(g_):>5} {100 * statistics.mean(x[BASE][0] for x in g_):>+17.1f}c {100 * loss / total_loss:>19.0f}% {100 * statistics.mean(x[BASE][0] / x['cost'] for x in g_):>+23.1f}%")
losers = [x[BASE][0] / x["cost"] for x in rows if x[BASE][0] < 0]
print(f"  a losing entry loses {100 * statistics.mean(losers):.0f}% of its stake on average (median {100 * statistics.median(losers):.0f}%, worst {100 * min(losers):.0f}%); {len(losers)} of {len(rows)} entries lose\n")
print("   loss controls swept on top of the lane (a loss line, from when it applies, and a time exit that sells a bet still not armed after N seconds)")
print(f"  {'control':34} {'mean net/contract':>18} {'per $ staked':>13} {'+-se':>6} {'avg loser (% stake)':>20} {'worst':>7} {'wins':>6}")
for st in STOPS:
    vals = per_dollar(rows, st)
    ls = [v for v in vals if v < 0]
    name = "none (the lane today)" if st == BASE else ((f"loss line {st[0]:.2f}" if st[0] else "") + (f" after {st[1]}s" if st[1] else "") + (f" + sell if unarmed at {st[2]}s" if st[2] and st[0] else (f"sell if unarmed at {st[2]}s" if st[2] else "")))
    print(f"  {name:34} {100 * statistics.mean(x[st][0] for x in rows):>+17.1f}c {100 * statistics.mean(vals):>+12.1f}% {100 * se_of(rows, vals):>5.1f}% {100 * statistics.mean(ls):>19.0f}% {100 * min(vals):>6.0f}% {100 * sum(v > 0 for v in vals) / len(vals):>5.0f}%")


def show(title, key, cuts):
    print(f"\n{title}")
    print(f"  {'group':26} {'n':>5} {'bid +6c':>8} {'avg peak':>9} {'net/contract':>13} {'per $ staked':>13} {'+-se':>6} {'avg loser':>10}")
    for name, lo, hi in cuts:
        r = [x for x in rows if x[key] is not None and lo <= x[key] < hi]
        if len(r) < 30:
            print(f"  {name:26} {len(r):>5}")
            continue
        v = per_dollar(r, BASE)
        ls = [a for a in v if a < 0]
        print(f"  {name:26} {len(r):>5} {100 * sum(x['peak'] >= UP - 1e-9 for x in r) / len(r):>7.0f}% {100 * statistics.mean(x['peak'] for x in r):>+8.1f}c {100 * statistics.mean(x[BASE][0] for x in r):>+12.1f}c {100 * statistics.mean(v):>+12.1f}% {100 * se_of(r, v):>5.1f}% {100 * statistics.mean(ls):>9.0f}%")


print("\nB. entry picking")
show("share of the last 10 s of taker dollars that bought THIS side", "share10", [("under 25%", 0, 0.25), ("25-50%", 0.25, 0.5), ("50-75%", 0.5, 0.75), ("75% or more", 0.75, 1.01)])
show("share of the last 30 s of taker dollars that bought THIS side", "share30", [("under 25%", 0, 0.25), ("25-50%", 0.25, 0.5), ("50-75%", 0.5, 0.75), ("75% or more", 0.75, 1.01)])
show("pace of this side's buying, last 10 s against the minute before", "pace", [("under 0.5x", 0, 0.5), ("0.5-1x", 0.5, 1), ("1-2x", 1, 2), ("2-4x", 2, 4), ("4x or more", 4, 1e9)])
show("dollars bought of this side in the last 10 s", "dollars10", [("under $5", 0, 5), ("$5-20", 5, 20), ("$20-100", 20, 100), ("$100 or more", 100, 1e9)])
show("resting book: this side's bid size over (bid + ask size)", "book", [("under 25%", 0, 0.25), ("25-50%", 0.25, 0.5), ("50-75%", 0.5, 0.75), ("75% or more", 0.75, 1.01)])
