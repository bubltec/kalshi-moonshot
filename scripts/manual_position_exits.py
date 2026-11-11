"""Which sell rule would have done best on the positions the user opened by hand? (2026-11-11, user: yes, test them. Of 100 hand-opened 15-minute positions, the 25 sold by hand made +$189.96 and the 75 left to settlement made -$113.49: studies/results/2026-11-11-manual-scalps.txt. The bot is to apply one rule to anything the user opens.)
Question: on those same positions, from the second of the first buy, with the user's own size and price, what does each sell rule make? Read-only. The buy is taken as it happened; only the sale is replayed, on the tape's quotes for that market.
Fills: a resting sell fills at its own price when the side's bid reaches it (no fee); any other sale is at the bid, with the taker fee.
Rules (fixed before results):
  held: to settlement.          as traded: what really happened.
  target G: a sell resting at the price paid + G from the buy on, else held. G 0.10 / 0.18 / 0.25.
  target G + time-to-go L: the same, and with L seconds left a position whose bid net of the fee is under the price paid is sold. L 240 / 120 / 60.
  target 0.18 + give-up: the same target, sold at once if the bid falls to half the price paid.
  lock: once the bid has been 0.10 over the price paid, sell if it comes back to 0.03 over; else held.
  trail X: once the bid has been 0.05 over the price paid, sell when it falls X from its high. X 0.08 / 0.15.
  curve sale: the bot's own time-scaled curve sale (bid net of the fee at least the curve's chance + 0.15 * sqrt(seconds left / 900), 5 s), for positions with a spot index; else held.
Reported: dollars made on all positions with quotes, how many end ahead and behind, how many lose 90% or more of their cost, the same without the single largest position, and split by whether the user sold it by hand or let it run.
    scripts/cloud.sh run scripts/manual_position_exits.py
"""

import bisect
import collections
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
bot_ids = {r[0] for r in st.db.execute("SELECT order_id FROM orders WHERE order_id IS NOT NULL").fetchall()}
by = collections.defaultdict(list)
for f in st.db.execute("SELECT ts,ticker,book_side,count,yes_price,no_price,fee,order_id FROM fills ORDER BY ts").fetchall():
    by[f["ticker"]].append(f)
man = collections.defaultdict(list)
for m in st.db.execute("SELECT ts,ticker,side,price,count FROM manual_entries ORDER BY ts").fetchall():
    man[(m["ticker"], m["side"])].append(m)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


P = []
for (tk, side), ms in man.items():
    ser = tk.split("-")[0]
    if not ser.endswith("15M"):
        continue
    w = st.db.execute("SELECT close_ts, strike, result FROM windows WHERE ticker=?", (tk,)).fetchone()
    if not w or w["result"] not in ("yes", "no"):
        continue
    close, t0 = float(w["close_ts"]), float(ms[0]["ts"])
    n = sum(float(m["count"] or 0.0) for m in ms)
    cost = sum(float(m["price"] or 0.0) * float(m["count"] or 0.0) for m in ms)
    if n <= 0 or cost <= 0:
        continue
    paid = cost / n
    yes = side == "yes"
    sell_side = "ask" if yes else "bid"
    sells = [f for f in by.get(tk, []) if f["book_side"] == sell_side and t0 < float(f["ts"]) < close and f["order_id"] not in bot_ids]
    px = lambda f: float(f["yes_price"]) if yes else (float(f["no_price"]) if f["no_price"] is not None else 1.0 - float(f["yes_price"]))  # noqa: E731
    raw = sum(float(f["count"]) for f in sells)
    sq = min(raw, n)
    proceeds = sum(px(f) * float(f["count"]) for f in sells) * (sq / raw if raw else 0.0)
    won = 1.0 if w["result"] == side else 0.0
    actual = proceeds + (n - sq) * won - cost - sum(float(f["fee"] or 0.0) for f in sells)
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, t0 - 5, close)).fetchall()
    if len(q) < 5:
        continue
    qt = [float(x[0]) for x in q]
    bids = []  # (second, left, bid of our side) each second from the buy
    for t in range(int(t0) + 1, int(close)):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bids.append((t, close - t, float(q[i][1]) if yes else 1.0 - float(q[i][2])))
    if len(bids) < 5:
        continue
    # the curve's chance each second, where there is a spot index
    chance = {}
    ix = variants.crypto_index(ser) or variants.biquote_index(ser)
    if ix:
        tkk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 900 - 1000, close)).fetchall()
        if len(tkk) > 1200:
            T_ = [float(x[0]) for x in tkk]
            base = int(close) - 1900
            g, j, last = [], 0, None
            for t in range(base, int(close) + 1):
                while j < len(T_) and T_[j] <= t:
                    last = float(tkk[j][1])
                    j += 1
                g.append(math.log(last) if last else None)
            ref = None
            if variants.crypto_index(ser):
                ref = math.log(float(w["strike"])) if w["strike"] and float(w["strike"]) > 0 else None
            elif g[1000] is not None:
                ref = g[1000]
            if ref is not None and all(x is not None for x in g[40:]):
                c60 = [0.0] * len(g)
                for k in range(100, len(g)):
                    c60[k] = c60[k - 1] + (g[k] - g[k - 60]) ** 2 / 60.0
                for t, left, bid in bids:
                    k = t - base
                    if k - 900 >= 100 and left > 0:
                        r = math.sqrt(max(c60[k] - c60[k - 900], 0.0) / 900.0)
                        c = (g[k] - ref) * (1.0 if yes else -1.0)
                        chance[t] = phi(c / (r * math.sqrt(left))) if r > 0 else (1.0 if c > 0 else 0.0)
    P.append({"tk": tk, "n": n, "cost": cost, "paid": paid, "won": won, "actual": actual, "hand": sq >= 0.9 * n, "bids": bids, "chance": chance})

hold = lambda p: p["n"] * p["won"] - p["cost"]  # noqa: E731


def taker(p, bid):
    return p["n"] * (bid - fee(bid)) - p["cost"] if bid > 0 else -p["cost"]


def rule_target(p, G, L=None, giveup=False):
    for t, left, bid in p["bids"]:
        if left < 8:
            break
        if bid >= p["paid"] + G:
            return p["n"] * (p["paid"] + G) - p["cost"]
        if giveup and bid <= p["paid"] / 2:
            return taker(p, bid)
        if L is not None and left <= L and bid - fee(bid) < p["paid"]:
            return taker(p, bid)
    return hold(p)


def rule_lock(p):
    armed = False
    for t, left, bid in p["bids"]:
        if left < 8:
            break
        if bid >= p["paid"] + 0.10:
            armed = True
        elif armed and bid <= p["paid"] + 0.03:
            return taker(p, bid)
    return hold(p)


def rule_trail(p, X):
    high = None
    for t, left, bid in p["bids"]:
        if left < 8:
            break
        if bid >= p["paid"] + 0.05:
            high = bid if high is None else max(high, bid)
        if high is not None and bid <= high - X:
            return taker(p, bid)
    return hold(p)


def rule_curve(p):
    since = None
    for t, left, bid in p["bids"]:
        ch = p["chance"].get(t)
        if left < 8 or ch is None or bid < 0.05:
            since = None
            continue
        if bid - fee(bid) - ch >= 0.15 * math.sqrt(min(left, 900.0) / 900.0):
            since = since if since is not None else t
            if t - since >= 5:
                return taker(p, bid)
        else:
            since = None
    return hold(p)


def rule_step(p, G, floor):
    """A sell resting from the buy on, repriced every 15 s: the price paid + G * sqrt(seconds left / seconds left at the buy), so the ask steps down as the clock runs (user, 2026-11-11: "set sell orders and adjust them as time runs out"). floor "paid": never under the price paid, then held; floor "none": it keeps stepping down under the price paid in the last L0/4 seconds toward the bid."""
    left0 = p["bids"][0][1]
    price = None
    for t, left, bid in p["bids"]:
        if left < 8:
            break
        if price is None or t % 15 == 0:
            price = p["paid"] + G * math.sqrt(max(left, 0.0) / left0)
            if floor == "none" and left <= left0 / 4.0:
                price = min(price, bid + 0.02)  # in the last quarter the order follows the bid down, two cents over it
        if bid >= price:
            return p["n"] * price - p["cost"]
    return hold(p)


def rule_curve_rest(p, far):
    """The time-scaled curve sale as a RESTING order: an ask at the curve's chance + far * sqrt(seconds left / 900), repriced every 15 s, filled when the bid reaches it (no fee, no 5 s wait)."""
    price = None
    for t, left, bid in p["bids"]:
        ch = p["chance"].get(t)
        if left < 8 or ch is None:
            continue
        if price is None or t % 15 == 0:
            price = min(0.99, ch + far * math.sqrt(min(left, 900.0) / 900.0))
        if bid >= price and price >= 0.05:
            return p["n"] * price - p["cost"]
    return hold(p)


RULES = [("as traded", lambda p: p["actual"]), ("held to settlement", hold)]
RULES += [(f"stepping ask from +{G:.2f}, floor {fl}", lambda p, G=G, fl=fl: rule_step(p, G, fl)) for G in (0.18, 0.30, 0.45) for fl in ("paid", "none")]
RULES += [(f"resting ask at chance + {far:.2f} scaled", lambda p, far=far: rule_curve_rest(p, far)) for far in (0.10, 0.15, 0.25)]
RULES += [(f"target +{G:.2f}, else held", lambda p, G=G: rule_target(p, G)) for G in (0.10, 0.18, 0.25)]
RULES += [(f"target +{G:.2f} + time-to-go {L}s", lambda p, G=G, L=L: rule_target(p, G, L)) for G in (0.10, 0.18, 0.25) for L in (240, 120, 60)]
RULES += [("target +0.18 + give-up at half", lambda p: rule_target(p, 0.18, None, True)), ("lock (+0.10 then back to +0.03)", rule_lock)]
RULES += [(f"trail {X:.2f} from the high", lambda p, X=X: rule_trail(p, X)) for X in (0.08, 0.15)]
RULES += [("time-scaled curve sale", rule_curve)]
big = max(P, key=lambda p: p["cost"]) if P else None
bigwin = max(P, key=lambda p: abs(hold(p))) if P else None
print(f"hand-opened 15-minute positions, settled, with quotes on the tape: {len(P)} | staked {sum(p['cost'] for p in P):.2f} | with a curve reading {sum(1 for p in P if p['chance'])} | sold by hand {sum(1 for p in P if p['hand'])}, left to run {sum(1 for p in P if not p['hand'])}")
if bigwin:
    print(f"the position that swings the totals most: {bigwin['tk']} cost {bigwin['cost']:.2f}, held result {hold(bigwin):+.2f}")
out = []
for name, fn in RULES:
    res = [(p, fn(p)) for p in P]
    tot = sum(x for _, x in res)
    out.append((tot, name, res))
out.sort(key=lambda o: -o[0])
print(f"{'rule':38s} | made (all) | ahead / behind | lost 90%+ | without the biggest swing | on the {sum(1 for p in P if p['hand'])} sold by hand | on the {sum(1 for p in P if not p['hand'])} left to run")
for tot, name, res in out:
    ahead = sum(1 for _, x in res if x > 0)
    behind = sum(1 for _, x in res if x < 0)
    wiped = sum(1 for p, x in res if x <= -0.9 * p["cost"])
    print(f"{name:38s} | {tot:+9.2f} | {ahead:3d} / {behind:3d}     | {wiped:3d}       | {sum(x for p, x in res if p is not bigwin):+9.2f}                | {sum(x for p, x in res if p['hand']):+9.2f}           | {sum(x for p, x in res if not p['hand']):+9.2f}")
print("DONE")
