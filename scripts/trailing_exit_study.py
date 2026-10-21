"""Peak-crash exit on quote ticks: once a position is up `act`, sell if the bid falls `drop` from its peak. Nothing is traded.

Entry: the cheap side (or the favorite) at minute 4/7/10 of a 15-minute window, at the ask shown by the recorded ticks.
Exit: walking the later ticks, track the peak bid; once the peak is `act` above the price paid, the first tick whose bid has
fallen `drop` below the peak is the sale, at that tick's bid (fees on both legs). Otherwise the contract settles.
    python scripts/trailing_exit_study.py moonshot.db tape.db
"""
import bisect, sqlite3, sys
from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1]); tape = sqlite3.connect(sys.argv[2])
wins = db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
cache = {}
def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]

def entries(lo, hi):
    out = []
    for tk, close, result in wins:
        ts, rows = quotes(tk)
        if not ts or ts[0] > close - 900 + 4 * 60:
            continue
        for m in (4, 7, 10):
            t0 = close - (15 - m) * 60
            i = bisect.bisect_right(ts, t0) - 1
            if i < 0:
                continue
            for side in ("yes", "no"):
                price = rows[i][2] if side == "yes" else 1 - rows[i][1]
                if lo <= price <= hi:
                    path = [(x[0], x[1] if side == "yes" else 1 - x[2]) for x in rows[i + 1:] if x[0] < close - 60]
                    out.append((close, tk, price, path, result == side))
    return out

def play(price, path, won, act, drop):
    peak = price
    for _, bid in path:
        peak = max(peak, bid)
        if drop is not None and peak >= price * (1 + act) - 1e-9 and bid <= peak - drop + 1e-9:
            return bid - fee(bid)
    return 1.0 if won else 0.0

def roi(items, act, drop):
    cost = sum(p + fee(p) for _, _, p, _, _ in items)
    return sum(play(p, path, w, act, drop) for _, _, p, path, w in items) / cost - 1 if cost else float("nan")

for label, lo, hi in (("FAVORITES 70-84c", 0.70, 0.84), ("CHEAP 5-40c", 0.05, 0.40)):
    tr = entries(lo, hi)
    if len(tr) < 20:
        print(label, "too few entries on the tape"); continue
    cut = tr[int(len(tr) * 0.6)][0]
    a = [t for t in tr if t[0] < cut]; b = [t for t in tr if t[0] >= cut]
    print(f"\n{label}: {len(tr)} entries in {len({t[1] for t in tr})} windows (train {len(a)}, test {len(b)})")
    print(f"{'rule':44} {'train':>7} {'test':>7}")
    print(f"{'hold to settlement':44} {roi(a, 0, None):>+7.1%} {roi(b, 0, None):>+7.1%}")
    for act in (0.10, 0.25):
        for drop in (0.02, 0.04, 0.06, 0.10):
            print(f"{f'up {act:.0%}, then a {drop * 100:.0f}c fall from the peak':44} {roi(a, act, drop):>+7.1%} {roi(b, act, drop):>+7.1%}")
