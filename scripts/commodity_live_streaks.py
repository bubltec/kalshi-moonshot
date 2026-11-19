"""Live commodity bets in time order: the winning runs, and how often a run that long happens if every bet wins with the probability its price says (2026-11-19, user: "we wouldn't win 19+ times in a row on luck alone ... i think you're rushing to a solution to dismiss this").
Bets: live commodity bets (mode trade, action bet) with a settled result, from SINCE (default 2026-11-09 00:00 ET, the Monday the logic was rewritten) to now, one row per decision, in the order bought. A bet won when its side was the result. Reported: the count, win rate against the average ask, every run of WIN_MIN (default 8) or more straight wins with its dates, size of the bets and average ask, and the current run. Then the luck test: each bet is a coin that wins with the probability of its own ask (the price; fee ignored), simulated 20000 times over the same sequence of asks; the share of simulations with a run at least as long as the longest real one, and the average number of runs of 10 or more. The bets are not independent (several commodities close in the same window and move together), which makes long runs MORE likely than the simulation says, so the test is generous to luck; it is said so. Then the real runs against the simulated ones, and the bets just before and after the longest run.
Run on the host: scripts/cloud.sh run scripts/commodity_live_streaks.py [WIN_MIN=8] [SINCE=<epoch>]"""
import datetime
import os
import random

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SER = ("BRENT", "WTI", "NATGAS", "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM")
win_min = int(os.environ.get("WIN_MIN", "8"))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = [b for b in st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.lane, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.ts>? AND w.result IN ('yes','no') ORDER BY d.ts", (since,)).fetchall() if b["ticker"].split("-")[0][2:-3] in SER and b["ask"]]
asks = [float(b["ask"]) for b in rows]
won = [b["result"] == b["side"] for b in rows]
stk = [float(b["stake_usd"] or 0) for b in rows]
n = len(rows)
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets in order bought; won {sum(won)} ({100.0 * sum(won) / n:.1f}%) against an average ask of {100.0 * sum(asks) / n:.1f}c; stake ${sum(stk):.0f}.")
runs, i = [], 0
while i < n:
    if won[i]:
        j = i
        while j + 1 < n and won[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    else:
        i += 1
longest = max((j - i + 1 for i, j in runs), default=0)
print(f"runs of {win_min} or more straight wins:")
for i, j in runs:
    if j - i + 1 >= win_min:
        seg = range(i, j + 1)
        print(f"  {j - i + 1:3d} wins  {datetime.datetime.fromtimestamp(float(rows[i]['ts']), ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(float(rows[j]['ts']), ET):%m-%d %H:%M} ET | avg ask {100.0 * sum(asks[k] for k in seg) / (j - i + 1):.1f}c | avg stake ${sum(stk[k] for k in seg) / (j - i + 1):.2f} | lanes " + ",".join(sorted({rows[k]['lane'] for k in seg})) + f" | chance every one wins at its price {100.0 * __import__('math').prod(asks[k] for k in seg):.2f}%")
cur = 0
while cur < n and won[n - 1 - cur]:
    cur += 1
print(f"longest real run {longest}; the current run is {cur} wins (still open if the last bet won).")
random.seed(7)
trials, hit, ten, nineteen = 20000, 0, 0, 0
for _ in range(trials):
    best = cur_ = 0
    r10 = 0
    for a in asks:
        if random.random() < a:
            cur_ += 1
            best = max(best, cur_)
            if cur_ == 10:
                r10 += 1
            if cur_ == 19:
                nineteen += 1
        else:
            cur_ = 0
    hit += best >= longest
    ten += r10
print(f"luck test: if every bet wins at its own price, a run of {longest} or longer happens in {100.0 * hit / trials:.1f}% of {trials} simulations of this sequence; runs of 10 or more average {ten / trials:.2f} a simulation (real: {sum(1 for i, j in runs if j - i + 1 >= 10)}); runs of 19 or more average {nineteen / trials:.2f} (real: {sum(1 for i, j in runs if j - i + 1 >= 19)}).")
exp_wins = sum(asks)
print(f"the whole record: {sum(won)} wins against {exp_wins:.1f} expected from the prices (standard deviation {sum(a * (1 - a) for a in asks) ** 0.5:.1f}).")
if runs:
    i, j = max(runs, key=lambda r: r[1] - r[0])
    lo, hi = max(0, i - 15), min(n, j + 16)
    print(f"the bets around the longest run (W/L ask stake, ET):")
    for k in range(lo, hi):
        print(f"  {datetime.datetime.fromtimestamp(float(rows[k]['ts']), ET):%m-%d %H:%M} {'W' if won[k] else 'L'} {asks[k]:.2f} ${stk[k]:.2f} {rows[k]['ticker'].split('-')[0]} {rows[k]['lane']}{'  <- run' if i <= k <= j else ''}")
print("DONE")
