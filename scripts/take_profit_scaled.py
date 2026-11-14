"""Take a profit that scales with the time left (2026-11-13, user: "I mostly just want to test out the taking profit concept like other functions it should scale as time runs out. With a lot of time left, more than 6 mins, take profit; 6-3 mins only sell if it hits 99c; after that let the normal logic ride. These numbers should be on a scale, the figures I gave are examples").
Question: on the five coin markets since Monday 11-09 00:00 ET, does adding a profit take whose bar rises as time runs out, and which stands down in the last minutes, beat the same bets with only the live price stop, and beat holding?
Entry (hypothetical, not the lane's own buys): the first second from minute 2 to minute 9 at which a side is priced 0.60 to 0.90 with a spread of 4 cents or less, bought at its ask. One contract. The exit the live lanes have is kept in every variant: the price stop (loss reaches 0.5 x the net bid of the cost, bid at least 5 cents). Anything else holds to the close, which pays 1 if the side won.
The profit take, two shapes. s_far is the share of the room between the all-in cost and a dollar that the gain must reach while there is a lot of time left (0.25 = a quarter of what could still be made).
  step (the user's example): more than 6 minutes left: sell when the share reaches s_far; 6 to 3 minutes: sell only when the bid is 0.99 or more; under 3 minutes: no profit take.
  smooth: the bar slides in a straight line from the s_far level at 10 minutes left to a bid of 0.99 at 3 minutes left; under 3 minutes: no profit take.
Rebuy (off, or on): after a sale, buy again at the ask when it is in the 0.60-0.90 band and has fallen at least 0.5 x the contract's typical 30-second move under the last sale price, with more than 60 seconds left, at most 4 buys.
Reported per variant: total over all markets (fee on every leg), against holding and against the stop alone, markets beat and lost; how many profit sales, and for those, what was given up (the market then won) and what was saved (it then lost); by curve shape (the contract's path efficiency over the 2 minutes before the entry, in thirds) and by time left at the entry.
Settings chosen before the run: s_far 0.25, 0.40, 0.60; shape step or smooth; rebuy off or on. Nothing is fitted.
    scripts/cloud_job.sh scripts/take_profit_scaled.py
"""

import calendar
import datetime
import math
import os
import re
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SINCE = calendar.timegm((2026, 11, 9, 5, 0, 0))
BAND, MAX_SPREAD, MAX_BUYS, MIN_LEFT_REBUY, ENTRY_FROM, ENTRY_TO, N = (0.60, 0.90), 0.04, 4, 60, 120, 540, 900
OFF, MID, FAR = 180, 360, 600  # seconds left: the profit take stands down at OFF; the step's first stage ends at MID; the smooth bar starts at FAR
VARIANTS = [(shape, sf, rb) for shape in ("step", "smooth") for sf in (0.25, 0.40, 0.60) for rb in (False, True)]


def bar(shape, sf, paid, left):
    """The bid that triggers the profit take, or None when it is off."""
    if left <= OFF:
        return None
    first = paid + sf * (1.0 - paid)
    if shape == "step":
        return first if left > MID else 0.99
    w = min(1.0, max(0.0, (FAR - left) / (FAR - OFF)))
    return first + (0.99 - first) * w


def simulate(bid, ask, u, win, e, shape, sf, rebuy, take=True):
    """Total per contract, buys, and the profit sales as (net received, win)."""
    total, buys, sales = 0.0, 0, []
    state, paid, last_sale = "buy", None, None
    i = e
    while i < N - 1:
        left = N - i
        if state == "buy":
            a = ask[i]
            ok = BAND[0] <= a <= BAND[1] and ask[i] - bid[i] <= MAX_SPREAD
            if buys == 0:
                ok = True  # the first entry is at index e by construction
            elif last_sale is None or a > last_sale - 0.5 * u[i] or left <= MIN_LEFT_REBUY or buys >= MAX_BUYS:
                ok = False
            if ok:
                paid = a + fee(a)
                total -= paid
                buys += 1
                state = "hold"
            elif buys > 0 and not rebuy:
                break
        else:
            b = bid[i]
            net = b - fee(b)
            pnl = net - paid
            stop = b >= 0.05 and pnl <= -0.5 * max(net, 0.0)
            tgt = bar(shape, sf, paid, left) if take else None
            prof = tgt is not None and b >= tgt and pnl > 0
            if (stop or prof) and i + 1 < N:
                sb = bid[i + 1]
                got = sb - fee(sb)
                total += got
                last_sale = sb
                state = "buy"
                if prof and not stop:
                    sales.append((got, win))
                if not rebuy:
                    return total, buys, sales
        i += 1
    if state == "hold":
        total += win
    return total, buys, sales


rows = []
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker ~ ?", (SINCE, "^KX(BTC|ETH|SOL|XRP|DOGE)15M-")).fetchall()]
print(f"crypto markets with quotes since Monday: {len(tickers)}")
for ticker in tickers:
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        continue
    try:
        close = int(datetime.datetime(2000 + int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ET).timestamp())
    except ValueError:
        continue
    open_ = close - 900
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, open_, close + 20)).fetchall()
    if len(q) < 200:
        continue
    fin = [(float(r[1]) + float(r[2])) / 2.0 for r in q if float(r[0]) >= close - 15]
    if not fin or not (fin[-1] > 0.9 or fin[-1] < 0.1):
        continue
    yes_won = 1 if fin[-1] > 0.9 else 0
    yb, ya, j, last = [], [], 0, None
    for t in range(open_, close):
        while j < len(q) and q[j][0] <= t:
            last = (float(q[j][1]), float(q[j][2]))
            j += 1
        yb.append(last[0] if last else None)
        ya.append(last[1] if last else None)
    first = next((k for k, x in enumerate(yb) if x is not None), None)
    if first is None or first > ENTRY_FROM - 60 or len(yb) < N or any(x is None for x in yb[first:N]):
        continue
    for k in range(first):
        yb[k], ya[k] = yb[first], ya[first]
    e = side = None
    for k in range(ENTRY_FROM, ENTRY_TO):
        mid = (yb[k] + ya[k]) / 2.0
        if ya[k] - yb[k] <= MAX_SPREAD:
            if BAND[0] <= ya[k] <= BAND[1]:
                e, side = k, "yes"
                break
            if BAND[0] <= 1.0 - yb[k] <= BAND[1]:
                e, side = k, "no"
                break
    if e is None:
        continue
    if side == "yes":
        bid, ask, win = yb, ya, yes_won
        mids = [(yb[k] + ya[k]) / 2.0 for k in range(N)]
    else:
        bid, ask, win = [1.0 - a for a in ya], [1.0 - b for b in yb], 1 - yes_won
        mids = [1.0 - (yb[k] + ya[k]) / 2.0 for k in range(N)]
    d30 = [(mids[k] - mids[k - 30]) ** 2 for k in range(30, N)]
    pre = [0.0]
    for x in d30:
        pre.append(pre[-1] + x)
    u = []
    for k in range(N):
        hi = k - 30
        lo = max(0, hi - 270)
        u.append(max(math.sqrt((pre[hi] - pre[lo]) / (hi - lo)), 0.005) if hi - lo >= 30 else 0.02)
    path = sum(abs(mids[k] - mids[k - 1]) for k in range(max(1, e - 120), e))
    eff = abs(mids[e - 1] - mids[max(0, e - 120)]) / path if path > 0 else None
    entry = ask[e]
    hold = win - entry - fee(entry)
    stop_only = simulate(bid, ask, u, win, e, "step", 0.5, False, take=False)[0]
    res = {v: simulate(bid, ask, u, win, e, v[0], v[1], v[2]) for v in VARIANTS}
    rows.append({"hold": hold, "stop": stop_only, "res": res, "eff": eff, "left": N - e})
print(f"markets in the test: {len(rows)}")


def table(label, sub):
    n = len(sub)
    if n < 30:
        return
    h, st = sum(r["hold"] for r in sub), sum(r["stop"] for r in sub)
    print(f"\n=== {label}: {n} markets, one contract each | holding {h:+.2f} | the price stop alone {st:+.2f}")
    for v in VARIANTS:
        tot = sum(r["res"][v][0] for r in sub)
        bh = sum(1 for r in sub if r["res"][v][0] > r["hold"] + 1e-9)
        lh = sum(1 for r in sub if r["res"][v][0] < r["hold"] - 1e-9)
        bs = sum(1 for r in sub if r["res"][v][0] > r["stop"] + 1e-9)
        ls = sum(1 for r in sub if r["res"][v][0] < r["stop"] - 1e-9)
        sales = [x for r in sub for x in r["res"][v][2]]
        gave = sum(1.0 - g for g, w in sales if w == 1)
        saved = sum(g for g, w in sales if w == 0)
        won_after = sum(1 for g, w in sales if w == 1)
        print(f"  {v[0]:6s} s_far {v[1]:.2f} rebuy {'on ' if v[2] else 'off'}: total {tot:+7.2f} | vs stop alone {tot - st:+6.2f} (beat {bs}, lost {ls}) | vs holding {tot - h:+6.2f} (beat {bh}, lost {lh}) | profit sales {len(sales):3d}: then won {won_after} (gave up {gave:.2f}), then lost {len(sales) - won_after} (saved {saved:.2f})")


table("all markets", rows)
effs = sorted(r["eff"] for r in rows if r["eff"] is not None)
if effs:
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table("choppy (path efficiency, lowest third)", [r for r in rows if r["eff"] is not None and r["eff"] <= lo])
    table("middle third", [r for r in rows if r["eff"] is not None and lo < r["eff"] < hi])
    table("straight (highest third)", [r for r in rows if r["eff"] is not None and r["eff"] >= hi])
table("entered with 8 minutes or more left", [r for r in rows if r["left"] >= 480])
table("entered with under 8 minutes left", [r for r in rows if r["left"] < 480])
print("DONE")
