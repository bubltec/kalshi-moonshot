"""Take a profit that scales with the time left, on top of the live crypto exit, with harsher fills (2026-11-14, user: "Yes" to rerunning with the live exit's hour-view check in place of the crude stop, and a fill haircut).
Question: on the five coin markets since Monday 11-09 00:00 ET (tape up to the newest dump), does adding the scaled profit take to the exit the crypto lanes run now beat that exit alone, and beat holding?
Entry (hypothetical, as before): the first second from minute 2 to minute 9 at which a side is priced 0.60 to 0.90 with a spread of 4 cents or less, bought at its ask. One contract.
The live exit, as the crypto lanes run it (curverisk.py, curve_risk_confirm 0.4): q is the net bid. Sell at once when the loss reaches 0.5 x q of the cost (bid at least 5 cents); or when q is under 0.5 for a wait of min(15 s x (1 - how far under), 0.25 x the time left), for a loss or a profit of at least 0.01 x q. Either sale also needs the hour view of the coin's settlement index to say the bet is lost: curverisk.long_chance under 0.4 (the real function, the index's one-minute moves over the last hour against its value at the window open). No hour view available: the price alone decides. No sale in the last 8 seconds.
The profit take (smooth and step shapes, s_far 0.25 and 0.40), as in take_profit_scaled.py: the bar rises from a share of the room at 10 minutes left to a bid of 0.99 at 3 minutes left (step: s_far over 6 minutes, 0.99 from 6 to 3), and none in the last 3 minutes. It is not gated by the hour view. Rebuy off or on, as before.
Fills: a sale or a rebuy decided at second i fills at the WORSE of the quotes at i+1 and i+2 (the lowest bid, the highest ask), so a spike that is gone a second later does not count. A rebuy whose price is then over 0.90 does not fill.
Reported: totals (fee on every leg) for holding, the live exit alone and each variant; against each; markets beat and lost; the profit sales and what they gave up or saved; by curve shape (path efficiency over the 2 minutes before the entry, in thirds). Settings chosen before the run, nothing fitted.
    scripts/cloud_job.sh scripts/take_profit_live_exit.py
"""

import calendar
import datetime
import math
import os
import re
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, curverisk, db
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SINCE = calendar.timegm((2026, 11, 9, 5, 0, 0))
BAND, MAX_SPREAD, MAX_BUYS, MIN_LEFT_REBUY, ENTRY_FROM, ENTRY_TO, N = (0.60, 0.90), 0.04, 4, 60, 120, 540, 900
OFF, MID, FAR, GUARD = 180, 360, 600, 8
CONFIRM = 0.4
DELAY = int(os.environ.get("DELAY", "0"))  # seconds after a sale before a rebuy may fill (2026-11-14, user: a low rebuy delay, 15 s if anything)
INDEX = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI", "XRP": "XRPUSD_RTI", "DOGE": "DOGEUSD_RTI"}
VARIANTS = [(shape, sf, rb) for shape in ("step", "smooth") for sf in (0.25, 0.40) for rb in (False, True)]


def bar(shape, sf, paid, left):
    if left <= OFF:
        return None
    first = paid + sf * (1.0 - paid)
    if shape == "step":
        return first if left > MID else 0.99
    w = min(1.0, max(0.0, (FAR - left) / (FAR - OFF)))
    return first + (0.99 - first) * w


def simulate(bid, ask, u, win, e, hour_ok, take=None, rebuy=False):
    """(total, buys, profit sales as (net received, win), counts of sales by kind). `take` is (shape, s_far) or None."""
    total, buys, sales, kinds = 0.0, 0, [], {"stop": 0, "line": 0, "profit": 0}
    state, paid, last_sale, since, sold_i = "buy", None, None, None, -10**9
    i = e
    while i < N - 3:
        left = N - i
        if state == "buy":
            a = ask[i]
            if buys == 0:
                fill = a
            else:
                ok = last_sale is not None and i - sold_i >= DELAY and a <= last_sale - 0.5 * u[i] and BAND[0] <= a <= BAND[1] and left > MIN_LEFT_REBUY and buys < MAX_BUYS
                if not ok:
                    if not rebuy:
                        break
                    i += 1
                    continue
                fill = max(ask[i + 1], ask[i + 2])
                if fill > BAND[1]:
                    i += 1
                    continue
            paid = fill + fee(fill)
            total -= paid
            buys += 1
            state, since = "hold", None
        else:
            b = bid[i]
            net = b - fee(b)
            pnl = net - paid
            q = min(max(net, 0.0), 1.0)
            kind = None
            if left > GUARD:
                if b >= 0.05 and pnl <= -0.5 * q and hour_ok(i):
                    kind = "stop"
                elif q < 0.5 and (pnl >= 0.01 * q or pnl < 0.0) and hour_ok(i):
                    sev = min(1.0, max(0.0, (0.5 - q) / 0.5))
                    since = i if since is None else since
                    if i - since >= max(0.0, min(15.0 * (1.0 - sev), 0.25 * left)):
                        kind = "line"
                else:
                    since = None
                if kind is None and take is not None:
                    tgt = bar(take[0], take[1], paid, left)
                    if tgt is not None and b >= tgt and pnl > 0:
                        kind = "profit"
            if kind:
                sb = min(bid[i + 1], bid[i + 2])
                got = sb - fee(sb)
                total += got
                last_sale, state, since, sold_i = sb, "buy", None, i
                kinds[kind] += 1
                if kind == "profit":
                    sales.append((got, win))
                if not rebuy:
                    return total, buys, sales, kinds
        i += 1
    if state == "hold":
        total += win
    return total, buys, sales, kinds


rows = []
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker ~ ?", (SINCE, "^KX(BTC|ETH|SOL|XRP|DOGE)15M-")).fetchall()]
print(f"crypto markets with quotes since Monday: {len(tickers)}")
no_hour = 0
for ticker in tickers:
    m = re.match(r"^KX([A-Z]+)15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        continue
    try:
        close = int(datetime.datetime(2000 + int(m.group(2)), MONTHS.index(m.group(3)) + 1, int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=ET).timestamp())
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
    rows_ix = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (INDEX[m.group(1)], open_ - 3700, close)).fetchall()
    its, ivs = [float(r[0]) for r in rows_ix], [float(r[1]) for r in rows_ix]
    ref = next((v for t, v in zip(its, ivs) if t >= open_), 0.0) if its and next((t for t in its if t >= open_), None) is not None and next(t for t in its if t >= open_) - open_ <= 60 else 0.0
    cache = {}

    def hour_ok(i, its=its, ivs=ivs, ref=ref, open_=open_, side=side, cache=cache):
        if i not in cache:
            c = curverisk.long_chance(its, ivs, open_ + i, ref, side, N - i) if ref > 0 and its else None
            cache[i] = True if c is None else c < CONFIRM  # no hour view: the price alone decides
        return cache[i]

    if ref <= 0 or len(its) < 600:
        no_hour += 1
    entry = ask[e]
    hold = win - entry - fee(entry)
    base = simulate(bid, ask, u, win, e, hour_ok)
    res = {v: simulate(bid, ask, u, win, e, hour_ok, (v[0], v[1]), v[2]) for v in VARIANTS}
    rows.append({"hold": hold, "exit": base[0], "exit_kinds": base[3], "res": res, "eff": eff})
print(f"markets in the test: {len(rows)} (of them {no_hour} with no hour of index ticks: the price alone decides there)")


def table(label, sub):
    n = len(sub)
    if n < 30:
        return
    h, ex = sum(r["hold"] for r in sub), sum(r["exit"] for r in sub)
    k = {x: sum(r["exit_kinds"][x] for r in sub) for x in ("stop", "line")}
    print(f"\n=== {label}: {n} markets, one contract each | holding {h:+.2f} | the live exit alone {ex:+.2f} (max-loss sales {k['stop']}, line sales {k['line']})")
    for v in VARIANTS:
        tot = sum(r["res"][v][0] for r in sub)
        be = sum(1 for r in sub if r["res"][v][0] > r["exit"] + 1e-9)
        le = sum(1 for r in sub if r["res"][v][0] < r["exit"] - 1e-9)
        bh = sum(1 for r in sub if r["res"][v][0] > r["hold"] + 1e-9)
        lh = sum(1 for r in sub if r["res"][v][0] < r["hold"] - 1e-9)
        sales = [x for r in sub for x in r["res"][v][2]]
        gave = sum(1.0 - g for g, w in sales if w == 1)
        saved = sum(g for g, w in sales if w == 0)
        wa = sum(1 for g, w in sales if w == 1)
        print(f"  {v[0]:6s} s_far {v[1]:.2f} rebuy {'on ' if v[2] else 'off'}: total {tot:+7.2f} | vs the live exit {tot - ex:+6.2f} (beat {be}, lost {le}) | vs holding {tot - h:+6.2f} (beat {bh}, lost {lh}) | profit sales {len(sales):3d}: then won {wa} (gave up {gave:.2f}), then lost {len(sales) - wa} (saved {saved:.2f})")


table("all markets", rows)
effs = sorted(r["eff"] for r in rows if r["eff"] is not None)
if effs:
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table("choppy (path efficiency, lowest third)", [r for r in rows if r["eff"] is not None and r["eff"] <= lo])
    table("middle third", [r for r in rows if r["eff"] is not None and lo < r["eff"] < hi])
    table("straight (highest third)", [r for r in rows if r["eff"] is not None and r["eff"] >= hi])
print("DONE")
