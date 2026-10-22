"""Peak-crash exit variants on quote ticks, for favorites bought mid-window. Replays recorded ticks; places nothing.

Rule: once the bid has been `act` above the price paid (a peak must have held 3 s), sell when it is `drop` below that peak
for `confirm` seconds. Variants: a fast exit (sell at once when the bid is `fast` below the peak, so a gap down is not
waited out) and whether a sale below the price paid is allowed (the live guard refuses it).
    python scripts/trail_variants_study.py moonshot.db tape.db
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


E = []
for tk, close, res in wins:
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
                path = [(x[0], x[1] if side == "yes" else 1 - x[2]) for x in rows[i + 1:] if x[0] < close - 20]
                E.append((close, price, path, res == side))


def play(price, path, won, act, drop, confirm, fast, guard):
    peak = 0.0
    last_t, last_b = None, 0.0
    below = None
    for t, b in path:
        if last_t is not None and t - last_t >= 3.0:
            peak = max(peak, last_b)  # the previous bid held at least 3 s
        last_t, last_b = t, b
        armed = peak >= price * (1 + act)
        if not armed:
            continue
        if fast and b <= peak - fast:
            return (b - fee(b)) if (b > price or not guard) else None, True
        if b <= peak - drop:
            below = t if below is None else below
            if t - below >= confirm:
                if b > price or not guard:
                    return b - fee(b), True
        else:
            below = None
    return None, False


def roi(items, **k):
    cost = ret = 0.0
    sold = 0
    for _, p, path, w in items:
        cost += p + fee(p)
        got, hit = play(p, path, w, **k)
        if hit and got is not None:
            ret += got
            sold += 1
        else:
            ret += 1.0 if w else 0.0
    return ret / cost - 1, sold


cut = E[int(len(E) * 0.6)][0]
tr = [e for e in E if e[0] < cut]
te = [e for e in E if e[0] >= cut]
hold = lambda g: sum(w for *_, w in g) / sum(p + fee(p) for _, p, _, _ in g) - 1  # noqa: E731
print(f"{len(E)} favorite entries; hold: train {hold(tr):+.1%}, test {hold(te):+.1%}\n")
print(f"{'variant':58} {'train':>7} {'test':>7} {'sold':>5}")
for label, kw in (
    ("as live: 10% / 4c / confirm 10s / never below cost", dict(act=0.10, drop=0.04, confirm=10, fast=0, guard=True)),
    ("allow a sale below cost", dict(act=0.10, drop=0.04, confirm=10, fast=0, guard=False)),
    ("+ fast exit at 8c off the peak", dict(act=0.10, drop=0.04, confirm=10, fast=0.08, guard=False)),
    ("+ fast exit at 12c off the peak", dict(act=0.10, drop=0.04, confirm=10, fast=0.12, guard=False)),
    ("+ fast exit at 16c off the peak", dict(act=0.10, drop=0.04, confirm=10, fast=0.16, guard=False)),
    ("fast 12c, confirm 20s, drop 6c", dict(act=0.10, drop=0.06, confirm=20, fast=0.12, guard=False)),
    ("fast 12c, arm at +15%", dict(act=0.15, drop=0.04, confirm=10, fast=0.12, guard=False)),
):
    a, _ = roi(tr, **kw)
    b, n = roi(te, **kw)
    print(f"{label:58} {a:>+7.1%} {b:>+7.1%} {n:>5}")
