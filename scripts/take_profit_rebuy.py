"""Take a profit on the way up and buy again on the way down: does the cumulative profit beat holding? (2026-11-13, user: "most markets there is a wave in the 15 mins ... if we are going to test multiple buys and a cumulative profit, open the time frame to 2,15".)
Question, one: on the five coin markets since Monday 11-09 00:00 ET, what does one contract bought at minute 2 and then traded by the rule below make in total (every leg, fee on every leg), against holding that same first contract to settlement and against the same first contract with the live price stop only?
Entry (hypothetical, not the lane's own buys, which start at minute 5): at minute 2, the side priced 0.60 to 0.90 (YES if its mid is in that band, NO if 1 - mid is) is bought at its ask, if the spread is at most 4 cents. Markets with neither side in the band are not in the test.
Rule, in the contract's own variance: u = the contract's typical 30-second move (rms of 30-second changes of its mid over the last 5 minutes). Holding: sell at the bid when the bid net of the sell fee is at least k_s x u over the all-in cost; or at the live price stop (loss reaches 0.5 x the net bid of the cost, bid at least 5 cents). Flat: buy again at the ask when it is in the 0.60-0.90 band and has fallen at least k_b x u under the last sale price, with more than 60 s left, at most 4 buys in all. Every order fills one second after the signal at that second's quote. Holding at the close pays 1 if the side won, else 0.
Settings, chosen before the run: k_s in 1, 2, 3 and k_b in 0.5, 1.0. Nothing is fitted. Result by curve shape (the contract's path efficiency over minutes 0 to 2, in thirds, known at the entry) and by entry price band; markets the rule beat and lost to holding.
Outcome from the last quote of the market (mid over 0.9 = YES, under 0.1 = NO; the rest skipped).
    scripts/cloud_job.sh scripts/take_profit_rebuy.py
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
BAND = (0.60, 0.90)
MAX_SPREAD, MAX_BUYS, MIN_LEFT, T0, T1 = 0.04, 4, 60, 120, 900
VARIANTS = [(ks, kb) for ks in (1.0, 2.0, 3.0) for kb in (0.5, 1.0)]


def run_rule(bid, ask, u, win, ks, kb, stop=True, take=True, rebuy=True):
    """Total profit per contract over the legs, and the number of buys. bid/ask/u are lists on a 1-second grid from T0; `win` is 1 if the side won."""
    n = len(bid)
    total, buys, legs = 0.0, 0, 0
    i = 0
    paid = None
    last_sale = None
    state = "buy"  # first buy at the first second
    while i < n - 1:
        if state == "buy":
            a = ask[i]
            if BAND[0] <= a <= BAND[1] and (buys == 0 or (last_sale is not None and a <= last_sale - kb * u[i] and n - i > MIN_LEFT)) and buys < MAX_BUYS:
                paid = a + fee(a)
                total -= paid
                buys += 1
                state = "hold"
            if buys == 0:
                return None  # the first entry must happen at second 0
            if state == "buy":
                if not rebuy:
                    break
        else:
            b = bid[i]
            net = b - fee(b)
            pnl = net - paid
            sell = (stop and b >= 0.05 and pnl <= -0.5 * max(net, 0.0)) or (take and pnl >= ks * u[i])
            if sell and i + 1 < n:
                sb = bid[i + 1]
                total += sb - fee(sb)
                last_sale = sb
                state = "buy"
                legs += 1
                if not rebuy:
                    return total, buys
        i += 1
    if state == "hold":
        total += win
    return total, buys


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
        if last is None:
            yb.append(None)
            ya.append(None)
        else:
            yb.append(last[0])
            ya.append(last[1])
    first = next((k for k, x in enumerate(yb) if x is not None), None)
    if first is None or first > T0 - 60 or len(yb) < T1 or any(x is None for x in yb[first:T1]):
        continue  # no quote early enough to read the contract's own movement before the entry, or a gap after it
    for k in range(first):
        yb[k], ya[k] = yb[first], ya[first]  # the first seconds before the first quote carry that quote
    mid0 = (yb[T0] + ya[T0]) / 2.0
    if BAND[0] <= mid0 <= BAND[1]:
        side, bid, ask, win = "yes", yb[T0:T1], ya[T0:T1], yes_won
        mids = [(yb[k] + ya[k]) / 2.0 for k in range(0, T1)]
    elif BAND[0] <= 1 - mid0 <= BAND[1]:
        side, bid, ask, win = "no", [1 - a for a in ya[T0:T1]], [1 - b for b in yb[T0:T1]], 1 - yes_won
        mids = [1 - (yb[k] + ya[k]) / 2.0 for k in range(0, T1)]
    else:
        continue
    if ask[0] - bid[0] > MAX_SPREAD or not (BAND[0] <= ask[0] <= BAND[1]):
        continue
    d30 = [(mids[k] - mids[k - 30]) ** 2 for k in range(30, len(mids))]
    pre = [0.0]
    for x in d30:
        pre.append(pre[-1] + x)
    u = []
    for k in range(T0, T1):
        hi = k - 30  # entries 0..hi-1 cover changes ending at 30..k-1
        lo = max(0, hi - 270)
        u.append(max(math.sqrt((pre[hi] - pre[lo]) / max(hi - lo, 1)), 0.005))
    path = sum(abs(mids[k] - mids[k - 1]) for k in range(1, T0))
    eff = abs(mids[T0 - 1] - mids[0]) / path if path > 0 else None
    entry = ask[0]
    hold = win - entry - fee(entry)
    stop_only = run_rule(bid, ask, u, win, 1.0, 1.0, stop=True, take=False, rebuy=False)[0]
    res = {v: run_rule(bid, ask, u, win, v[0], v[1]) for v in VARIANTS}
    rows.append({"hold": hold, "stop": stop_only, "res": res, "eff": eff, "entry": entry, "side": side})
print(f"markets in the test (a side in the band at minute 2, spread at most 4c): {len(rows)}")


def table(label, sub):
    n = len(sub)
    if n < 30:
        return
    print(f"\n=== {label}: {n} markets, one contract each")
    print(f"  holding to settlement {sum(r['hold'] for r in sub):+.2f} | the live price stop only {sum(r['stop'] for r in sub):+.2f}")
    for v in VARIANTS:
        tot = sum(r["res"][v][0] for r in sub)
        beat_hold = sum(1 for r in sub if r["res"][v][0] > r["hold"] + 1e-9)
        lost_hold = sum(1 for r in sub if r["res"][v][0] < r["hold"] - 1e-9)
        beat_stop = sum(1 for r in sub if r["res"][v][0] > r["stop"] + 1e-9)
        buys = statistics.fmean(r["res"][v][1] for r in sub)
        multi = sum(1 for r in sub if r["res"][v][1] >= 2)
        print(f"  k_sell {v[0]:.0f} k_rebuy {v[1]:.1f}: total {tot:+.2f} | vs holding {tot - sum(r['hold'] for r in sub):+.2f} (beat {beat_hold}, lost {lost_hold}) | vs the stop only {tot - sum(r['stop'] for r in sub):+.2f} (beat {beat_stop}) | buys per market {buys:.2f}, markets with a rebuy {multi}")


table("all markets", rows)
effs = sorted(r["eff"] for r in rows if r["eff"] is not None)
if effs:
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table("choppy (path efficiency in the lowest third)", [r for r in rows if r["eff"] is not None and r["eff"] <= lo])
    table("middle third", [r for r in rows if r["eff"] is not None and lo < r["eff"] < hi])
    table("straight (the highest third)", [r for r in rows if r["eff"] is not None and r["eff"] >= hi])
for a, b in ((0.60, 0.70), (0.70, 0.80), (0.80, 0.90)):
    table(f"entry priced {a:.2f}-{b:.2f}", [r for r in rows if a <= r["entry"] < b or (b == 0.90 and r["entry"] == 0.90)])
print("DONE")
