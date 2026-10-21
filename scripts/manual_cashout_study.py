"""Would a computer-speed cash-out rule have helped your own 15-minute trades? Replays each trade on the recorded quote ticks.

For every finished 15-minute trade of yours (or the bot's, with --source) that has quotes on the tape, walk the quote changes
between the buy and the trade's actual end. If a rule fires first, the trade is re-scored as sold at that bid (fee included);
otherwise it keeps its real result. Compare the totals with what actually happened. Nothing is traded.
    python scripts/manual_cashout_study.py moonshot.db tape.db [manual|bot|all]
"""
import bisect, sqlite3, sys
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee, spike_hit, take_profit_hit
from kalshi_moonshot.trips import build_trips

store = Store(sys.argv[1]); tape = sqlite3.connect(sys.argv[2])
source = sys.argv[3] if len(sys.argv) > 3 else "manual"
trips = [t for t in build_trips(store) if t.done and t.reliable and t.series.endswith("15M") and (source == "all" or t.source == source)]
cache = {}
def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]
usable = []
for t in trips:
    ts, rows = quotes(t.ticker)
    if ts and ts[0] <= t.opened_ts:
        usable.append(t)
print(f"{len(trips)} finished {source} 15-minute trades; {len(usable)} have quote ticks from before the buy\n")

def bid_of(t, q):
    return q[1] if t.side == "yes" else (None if q[2] is None else 1 - q[2])

def replay(t, x=0.0, cents=0.0, secs=30.0, part=1.0):
    ts, rows = quotes(t.ticker)
    i = bisect.bisect_right(ts, t.opened_ts)
    hist = []
    while i < len(ts) and ts[i] < t.closed_ts:
        bid = bid_of(t, rows[i])
        if bid:
            hist.append((ts[i], bid))
            if take_profit_hit(t.entry_price, bid, x) or spike_hit(hist, ts[i], t.entry_price, cents, secs):
                sold = t.contracts * (bid - fee(bid)) - t.cost - t.contracts * fee(t.entry_price)
                return part * sold + (1 - part) * t.pnl, ts[i] - t.opened_ts   # the rest keeps its real result
        i += 1
    return t.pnl, None

actual = sum(t.pnl for t in usable)
print(f"{'rule':34} {'sold early':>10} {'net P&L':>9} {'vs actual':>10}")
print(f"{'what actually happened':34} {sum(1 for t in usable if t.exit_type != 'settlement'):>10} {actual:>+9.2f}")
for name, kw in [("sell at 1.5x", dict(x=1.5)), ("sell at 2x", dict(x=2)), ("sell at 3x", dict(x=3)), ("sell at 4x", dict(x=4)),
                 ("spike +10c in 30s", dict(cents=0.10)), ("spike +15c in 30s", dict(cents=0.15)), ("spike +20c in 30s", dict(cents=0.20)),
                 ("3x or spike +10c", dict(x=3, cents=0.10)),
                 ("sell HALF at 2x, rest as played", dict(x=2, part=0.5)), ("sell HALF at 3x, rest as played", dict(x=3, part=0.5)),
                 ("sell HALF on spike +10c, rest as played", dict(cents=0.10, part=0.5)), ("sell HALF on spike +15c, rest as played", dict(cents=0.15, part=0.5))]:
    net = fired = 0.0
    for t in usable:
        pnl, when = replay(t, **kw)
        net += pnl; fired += when is not None
    print(f"{name:34} {int(fired):>10} {net:>+9.2f} {net - actual:>+10.2f}")
big = sorted(usable, key=lambda t: t.pnl)
print("\nyour five biggest losses, and what 3x / a 10c spike rule would have done:")
for t in big[:5]:
    a, _ = replay(t, x=3); b, w = replay(t, cents=0.10)
    print(f"  {t.ticker:30} {t.side.upper():3} paid {t.entry_price:.2f} actual {t.pnl:+7.2f} | 3x {a:+7.2f} | spike {b:+7.2f}" + ("" if w is None else f" (sold {w:.0f}s in)"))
