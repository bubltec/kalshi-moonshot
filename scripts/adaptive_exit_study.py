"""An exit that learns, per market, what a pivot usually leads to, instead of fixed thresholds. Walk-forward, read-only, pure Python.

THE MODEL. For a held side, at every second, the state is: its bid b, the seconds left L, how far the bid is under its best of the last 120 s measured in that market's own usual noise (the series' std of 5-second bid moves in the training days),
how far it has bounced off its low of the last 30 s (same units), and how many swings of 4c or more it made in the last 60 s (an oscillating market vs a clean trend). The chance the side still wins is learned from the training days as the
bid plus a correction, estimated in three layers that each shrink toward the one above when they have little data: (0) the win rate at that bid alone (how well priced the bid is), (1) all commodities together in that state,
(2) this one market (series) in that state. The shrinkage weight is n / (n + K) with n the states in the cell, so a market with little history leans on the pool and a market with a lot leans on its own record.
THE RULE. Sell when the learned chance is under what the sale pays by more than MARGIN: P(win) < b - fee(b) - MARGIN. No fixed fall, bounce or time is in the rule.
THE TEST. Markets are cut by close time: the model is learned on the earlier markets and the rule is run only on the later ones, entries being the favorite side (ask 0.78-0.92) SCAN minutes before the close (the lane's bets, without its
other choices) and the lane's own live bets in the test period. Scored as in exit_tradeoff.py: LOSS KEPT (share of the losing bets' dollars still lost, 100% = holding), GAVE UP (winnings given up by selling winners), NET (recovered minus gave
up), PRICE. Beside it: the Brier score of the learned chance against the bare bid on the test states (lower is better): if the model does not beat the bid, it knows nothing the market does not. Also scored on the same test bets: the rigid
drop-0.15 and bounce rules, for comparison.

    scripts/cloud.sh run scripts/adaptive_exit_study.py [SCAN=6] [K=40] [MARGIN=0.01]
"""
import bisect
import math
import os
import re
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SCAN = float(os.environ.get("SCAN", "6"))
K = float(os.environ.get("K", "40"))
MARGIN = float(os.environ.get("MARGIN", "0.01"))
WANT = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER", "WTI", "BRENT", "NATGAS"}
SPAN, STEP = 240, 3

markets = []  # dict per market
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > 0 ORDER BY close_ts").fetchall():
    m = re.match(r"^KX([A-Z]+)15M$", w["series"] or "")
    if not m or m.group(1) not in WANT:
        continue
    close = float(w["close_ts"])
    q = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - SPAN - 130, close + 1)).fetchall()]
    if len(q) < 30:
        continue
    ts = [x[0] for x in q]
    grids = {}
    for side in ("yes", "no"):
        g = []
        for k in range(SPAN + 130 + 1):
            t = close - SPAN - 130 + k
            i = bisect.bisect_right(ts, t) - 1
            if i < 0 or t - ts[i] > 8:
                g.append(None)
            else:
                g.append(q[i][1] if side == "yes" else 1.0 - q[i][2])
        grids[side] = g
    asks = {}
    for side in ("yes", "no"):
        i = bisect.bisect_right(ts, close - SCAN * 60) - 1
        asks[side] = (q[i][2] if side == "yes" else 1.0 - q[i][1]) if i >= 0 and close - SCAN * 60 - ts[i] <= 20 else None
    markets.append({"t": w["ticker"], "series": m.group(1), "close": close, "res": w["result"], "g": grids, "ask": asks, "off": SPAN + 130})
print(f"{len(markets)} settled commodity markets with quotes; {markets[0]['series'] if markets else ''} ... closes from {markets[0]['close']:.0f} to {markets[-1]['close']:.0f}\n")


def features(mk, side, t_idx, vs):
    """state of `side` at second index t_idx of its grid (index = seconds from the grid start), or None."""
    g = mk["g"][side]
    b = g[t_idx]
    if b is None or b <= 0.0:
        return None
    win = [x for x in g[max(0, t_idx - 120): t_idx + 1] if x is not None]
    low30 = [x for x in g[max(0, t_idx - 30): t_idx + 1] if x is not None]
    if len(win) < 20 or not low30:
        return None
    fall = (max(win) - b) / vs
    bounce = (b - min(low30)) / vs
    last60 = [x for x in g[max(0, t_idx - 60): t_idx + 1] if x is not None]
    swings, direction, ext = 0, 0, last60[0] if last60 else b
    for x in last60[1:]:
        if direction >= 0 and ext - x >= 0.04:
            swings, direction, ext = swings + 1, -1, x
        elif direction <= 0 and x - ext >= 0.04:
            swings, direction, ext = swings + 1, 1, x
        elif (direction >= 0 and x > ext) or (direction <= 0 and x < ext):
            ext = x
    left = (len(g) - 1) - t_idx
    return b, left, fall, bounce, swings


def cell(f):
    b, left, fall, bounce, swings = f
    lb = 0 if left < 30 else 1 if left < 60 else 2 if left < 120 else 3
    return (min(int(b * 5), 4), lb, 0 if fall < 1 else 1 if fall < 2 else 2 if fall < 4 else 3, 0 if bounce < 0.5 else 1 if bounce < 1.5 else 2, min(swings, 2))


def fine(b):
    return min(int(b * 20), 19)


def noise(mks):
    d = defaultdict(list)
    for mk in mks:
        for side in ("yes", "no"):
            g = mk["g"][side]
            d[mk["series"]] += [g[i] - g[i - 5] for i in range(mk["off"] - SPAN, mk["off"], 5) if g[i] is not None and g[i - 5] is not None]
    return {k: max(statistics.pstdev(v), 0.01) if len(v) > 20 else 0.03 for k, v in d.items()}


class Model:
    def __init__(self, train):
        self.vs = noise(train)
        self.default_vs = statistics.median(self.vs.values()) if self.vs else 0.03
        fin = defaultdict(lambda: [0, 0.0])      # fine bid bucket -> [n, sum(win - b)]
        pool = defaultdict(lambda: [0, 0.0])     # cell -> [n, sum(win - p0)]
        ser = defaultdict(lambda: [0, 0.0])      # (series, cell) -> [n, sum(win - p1)]
        rows = []
        for mk in train:
            vs = self.vs.get(mk["series"], self.default_vs)
            for side in ("yes", "no"):
                win = 1.0 if mk["res"] == side else 0.0
                for ti in range(mk["off"] - SPAN, mk["off"] - 10, STEP):
                    f = features(mk, side, ti, vs)
                    if f and 0.03 <= f[0] <= 0.97:
                        rows.append((mk["series"], f, win))
                        fin[fine(f[0])][0] += 1
                        fin[fine(f[0])][1] += win - f[0]
        self.fin = fin
        self.p0 = lambda b: min(max(b + (fin[fine(b)][1] / fin[fine(b)][0] if fin[fine(b)][0] >= 10 else 0.0), 0.0), 1.0)
        for sr, f, win in rows:
            c = cell(f)
            pool[c][0] += 1
            pool[c][1] += win - self.p0(f[0])
        self.pool = pool
        for sr, f, win in rows:
            c = cell(f)
            p1 = self.pool_p(f)
            ser[(sr, c)][0] += 1
            ser[(sr, c)][1] += win - p1
        self.ser = ser

    def pool_p(self, f):
        n, tot = self.pool.get(cell(f), (0, 0.0))
        p0 = self.p0(f[0])
        return min(max(p0 + (tot / n if n else 0.0) * n / (n + K), 0.0), 1.0)

    def p(self, series, f):
        n, tot = self.ser.get((series, cell(f)), (0, 0.0))
        p1 = self.pool_p(f)
        return min(max(p1 + (tot / n if n else 0.0) * n / (n + K), 0.0), 1.0)


def hold_pnl(ask, won, n=1.0):
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def run_rule(entries, decide):
    """entries: (mk, side, ask, contracts). decide(mk, side, ti) -> sale index or None. Returns the scoring tuple."""
    held_loss = kept = gave = 0.0
    sold = sl = sw = 0
    for mk, side, ask, n, t0 in entries:
        won = mk["res"] == side
        hold = hold_pnl(ask, won, n)
        sale_at = decide(mk, side, t0)
        sale = None
        if sale_at is not None:
            px = mk["g"][side][min(sale_at + 1, len(mk["g"][side]) - 1)] or mk["g"][side][sale_at]
            if px and px > 0:
                sale = (px - ask - fee_per_contract(ask) - fee_per_contract(px)) * n
        pnl = sale if sale is not None else hold
        if not won:
            held_loss += -hold
            kept += -pnl
        if sale is not None:
            sold += 1
            if won:
                sw += 1
                gave += hold - sale
            else:
                sl += 1
    rec = held_loss - kept
    return sold, sl, sw, (kept / held_loss if held_loss else 0.0), gave, rec - gave, (gave / rec * 100 if rec > 0 else float("nan")), held_loss


def drop_rule(mk, side, t0):
    g = mk["g"][side]
    for t in range(t0 + 6, len(g) - 1 - 15):
        a, b = g[t - 5], g[t]
        if a is not None and b is not None and a >= 0.80 and a - b >= 0.15 and (len(g) - 1 - t) <= 180:
            c = g[min(t + 4, len(g) - 1)]
            if c is not None and c <= a - 0.10:
                return t + 4
    return None


def bounce_rule(paid):
    def rule(mk, side, t0):
        g, low = mk["g"][side], None
        for t in range(t0 + 1, len(g) - 1 - 10):
            b = g[t]
            if b is None or b <= 0.0:
                continue
            low = b if low is None else min(low, b)
            if paid[id(mk)] - low >= 0.15 and b - low >= 0.04:
                return t
        return None
    return rule


def evaluate(train, test, label):
    model = Model(train)
    # Brier on the test states
    bs_model = bs_bid = nst = 0
    for mk in test:
        vs = model.vs.get(mk["series"], model.default_vs)
        for side in ("yes", "no"):
            win = 1.0 if mk["res"] == side else 0.0
            for ti in range(mk["off"] - SPAN, mk["off"] - 10, STEP):
                f = features(mk, side, ti, vs)
                if f and 0.03 <= f[0] <= 0.97:
                    p = model.p(mk["series"], f)
                    bs_model += (p - win) ** 2
                    bs_bid += (f[0] - win) ** 2
                    nst += 1
    print(f"== {label}: learned on {len(train)} markets, tested on {len(test)}; Brier on {nst} test states: model {bs_model / nst:.4f} vs bare bid {bs_bid / nst:.4f} ({'model better' if bs_model < bs_bid else 'bid better'})")
    entries, paid = [], {}
    for mk in test:
        for side in ("yes", "no"):
            ask = mk["ask"][side]
            if ask is not None and 0.78 <= ask <= 0.92:
                entries.append((mk, side, ask, 1.0, mk["off"] + SPAN - int(SCAN * 60)))
                paid[id(mk)] = ask
    vs_of = lambda mk: model.vs.get(mk["series"], model.default_vs)  # noqa: E731

    def adaptive(margin):
        def rule(mk, side, t0):
            for t in range(t0 + 1, len(mk["g"][side]) - 1 - 10):
                f = features(mk, side, t, vs_of(mk))
                if f and f[0] > 0.02 and model.p(mk["series"], f) < f[0] - fee_per_contract(f[0]) - margin:
                    return t
            return None
        return rule

    print(f"  {len(entries)} test entries (favorite side bought {SCAN:g} min out), {sum(1 for e in entries if e[0]['res'] != e[1])} of them lost")
    print(f"  {'rule':<24} | {'sold':>4} {'lost':>4} {'won':>4} | {'loss kept':>9} {'gave up':>8} {'NET':>7} {'price':>7}")
    for name, rule in (("adaptive margin 0.00", adaptive(0.0)), (f"adaptive margin {MARGIN:.2f}", adaptive(MARGIN)), ("adaptive margin 0.03", adaptive(0.03)), ("drop 0.15 + confirm", drop_rule), ("bounce 0.15 / 0.04", bounce_rule(paid))):
        r = run_rule(entries, rule)
        print(f"  {name:<24} | {r[0]:>4} {r[1]:>4} {r[2]:>4} | {r[3]:>8.0%} {r[4]:>+8.2f} {r[5]:>+7.2f} {r[6]:>6.0f}c")
    print()


n = len(markets)
if n >= 200:
    evaluate(markets[: int(n * 0.6)], markets[int(n * 0.6):], "split A: first 60% -> last 40%")
    evaluate(markets[: int(n * 0.4)], markets[int(n * 0.4): int(n * 0.7)], "split B: first 40% -> next 30%")
    evaluate(markets[int(n * 0.3): int(n * 0.8)], markets[int(n * 0.8):] + markets[: int(n * 0.3)][:0], "split C: middle 50% -> last 20%")
print("DONE")
