"""Your pattern: buy a side priced around 30 cents mid-window, ride it up, sell on the dip. Replays recorded ticks; places nothing.

Entry: a side whose ask is `lo`-`hi` at minute 4, 6, 8 or 10 (and, for the second table, only if the side's bid has risen over the
previous 90 seconds). Exit: once the bid has been `act` above the price paid (a peak must hold 3 s), sell when it is `drop` below that
peak for `confirm` seconds, or at once at `fast` below it. Otherwise it settles. It does not need to reach the target to profit.
    python scripts/buy30_trail_study.py moonshot.db tape.db [series_prefix_filter]
"""
import bisect
import sqlite3
import sys

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
flt = sys.argv[3] if len(sys.argv) > 3 else ""
wins = db.execute("select ticker, series, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
cache = {}


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


def entries(lo, hi, rising):
    out = []
    for tk, series, close, res in wins:
        if flt and not any(series.startswith(f) for f in flt.split(",")):
            continue
        ts, rows = quotes(tk)
        if not ts or ts[0] > close - 900 + 4 * 60:
            continue
        for m in (4, 6, 8, 10):
            t0 = close - (15 - m) * 60
            i = bisect.bisect_right(ts, t0) - 1
            j = bisect.bisect_right(ts, t0 - 90) - 1
            if i < 0 or j < 0:
                continue
            for side in ("yes", "no"):
                px = lambda r: r[2] if side == "yes" else 1 - r[1]  # noqa: E731
                bd = lambda r: r[1] if side == "yes" else 1 - r[2]  # noqa: E731
                price = px(rows[i])
                if lo <= price <= hi and (not rising or bd(rows[i]) - bd(rows[j]) >= 0.01):
                    path = [(x[0], bd(x)) for x in rows[i + 1:] if x[0] < close - 20]
                    out.append((close, price, path, res == side))
    return out


def play(price, path, won, act, drop, confirm, fast):
    peak, last_t, last_b, below = 0.0, None, 0.0, None
    for t, b in path:
        if last_t is not None and t - last_t >= 3.0:
            peak = max(peak, last_b)
        last_t, last_b = t, b
        if peak < price * (1 + act):
            continue
        if fast and b <= peak - fast:
            return b - fee(b)
        if b <= peak - drop:
            below = t if below is None else below
            if t - below >= confirm:
                return b - fee(b)
        else:
            below = None
    return 1.0 if won else 0.0


def roi(items, **k):
    cost = sum(p + fee(p) for _, p, _, _ in items)
    return sum(play(p, path, w, **k) for _, p, path, w in items) / cost - 1 if cost else float("nan")


for label, lo, hi, rising in (("25-35c, any", 0.25, 0.35, False), ("25-35c, bid rising into the buy", 0.25, 0.35, True),
                              ("20-40c, any", 0.20, 0.40, False)):
    E = entries(lo, hi, rising)
    if len(E) < 40:
        print(f"{label}: only {len(E)} entries")
        continue
    cut = E[int(len(E) * 0.6)][0]
    tr = [e for e in E if e[0] < cut]
    te = [e for e in E if e[0] >= cut]
    hold = lambda g: sum(w for *_, w in g) / sum(p + fee(p) for _, p, _, _ in g) - 1  # noqa: E731
    print(f"\n{label}: {len(E)} entries (train {len(tr)}, test {len(te)}); hold to settlement: train {hold(tr):+.1%}, test {hold(te):+.1%}")
    print(f"  {'trail rule':44} {'train':>7} {'test':>7}")
    for act, drop, confirm, fast in ((0.10, 0.04, 10, 0.12), (0.20, 0.05, 10, 0.12), (0.20, 0.08, 10, 0.15), (0.30, 0.08, 10, 0.15), (0.30, 0.10, 15, 0.20)):
        print(f"  {f'arm +{act:.0%}, {drop:.2f} off for {confirm}s, instant {fast:.2f}':44} "
              f"{roi(tr, act=act, drop=drop, confirm=confirm, fast=fast):>+7.1%} {roi(te, act=act, drop=drop, confirm=confirm, fast=fast):>+7.1%}")
