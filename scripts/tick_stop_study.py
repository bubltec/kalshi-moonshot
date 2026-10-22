"""A stop-loss that reacts on the quote tick, for favorites bought mid-window. Replays recorded ticks; places nothing.

Entry: a side priced 70-84 cents at minute 4, 6, 8 or 10 of a 15-minute window (the ask in the tape then). Exit rules:
hold to settlement, or sell at the first later tick whose bid has fallen `drop` below the price paid (at that tick's bid, a
real fill: in a gap the first bid can already be far below the stop). Tuned on the earliest 60% of windows, judged on the last 40%.
    python scripts/tick_stop_study.py moonshot.db tape.db
"""
import bisect
import sqlite3
import sys

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
wins = db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
cache = {}


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


entries = []
for tk, close, result in wins:
    ts, rows = quotes(tk)
    if not ts or ts[0] > close - 900 + 4 * 60:
        continue
    for m in (4, 6, 8, 10):
        t0 = close - (15 - m) * 60
        i = bisect.bisect_right(ts, t0) - 1
        if i < 0:
            continue
        for side in ("yes", "no"):
            price = rows[i][2] if side == "yes" else 1 - rows[i][1]
            if 0.70 <= price <= 0.84:
                path = [(x[0] - t0, x[1] if side == "yes" else 1 - x[2]) for x in rows[i + 1:] if x[0] < close - 20]
                entries.append((close, price, path, result == side))


def play(price, path, won, drop):
    if drop is not None:
        for _, bid in path:
            if bid <= price * (1 - drop):
                return bid - fee(bid)
    return 1.0 if won else 0.0


def roi(items, drop):
    cost = sum(p + fee(p) for _, p, _, _ in items)
    return sum(play(p, path, w, drop) for _, p, path, w in items) / cost - 1 if cost else float("nan")


cut = entries[int(len(entries) * 0.6)][0]
train = [e for e in entries if e[0] < cut]
test = [e for e in entries if e[0] >= cut]
print(f"{len(entries)} favorite entries (70-84c); train {len(train)}, test {len(test)}\n{'rule':34} {'train':>7} {'test':>7} {'sold':>6}")
print(f"{'hold to settlement':34} {roi(train, None):>+7.1%} {roi(test, None):>+7.1%}")
for drop in (0.10, 0.15, 0.20, 0.30, 0.40, 0.50):
    sold = sum(1 for _, p, path, w in test if play(p, path, w, drop) not in (0.0, 1.0))
    print(f"{f'sell at first tick {drop:.0%} under cost':34} {roi(train, drop):>+7.1%} {roi(test, drop):>+7.1%} {sold:>6}")
gaps = []
for _, p, path, _ in test:
    for _, bid in path:
        if bid <= p * 0.7:
            gaps.append(bid / p)
            break
if gaps:
    gaps.sort()
    print(f"\nwhen the 30% stop triggers, the first bid it sees is already at {gaps[len(gaps) // 2]:.0%} of the price paid (median); worst quarter {gaps[len(gaps) // 4]:.0%}")


# --- a follow-through check: if the bid is not above the price paid after `wait` seconds, sell it there ---
def bid_at(path, wait):
    last = None
    for t, bid in path:
        if t > wait:
            break
        last = bid
    return last


def roi_follow(items, wait, need):
    cost = ret = 0.0
    for _, p, path, w in items:
        cost += p + fee(p)
        b = bid_at(path, wait)
        ret += (b - fee(b)) if (b is not None and b < p + need) else (1.0 if w else 0.0)
    return ret / cost - 1 if cost else float("nan")


print(f"\n{'follow-through rule':44} {'train':>7} {'test':>7}")
for wait in (30, 60, 120):
    for need in (0.0, 0.03):
        print(f"{f'after {wait}s sell unless bid >= paid {need:+.2f}':44} {roi_follow(train, wait, need):>+7.1%} {roi_follow(test, wait, need):>+7.1%}")
red = [e for e in test if (bid_at(e[2], 60) or 1) < e[1]]
green = [e for e in test if (bid_at(e[2], 60) or 0) >= e[1]]
print(f"\nat 60s: {len(red)} bets were under their price (won {sum(e[3] for e in red) / max(1, len(red)):.0%}), "
      f"{len(green)} at or above (won {sum(e[3] for e in green) / max(1, len(green)):.0%})")
