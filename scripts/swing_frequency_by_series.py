"""How often does a 15-minute market's own curve offer the swing the user's hand scalps rode? (2026-11-21, user: "I think crypto markets tend to have this shape more often making it easier")
Question: per series, what share of 15-minute windows had at least one round trip that a side could be bought at the ask and sold at the bid within SPAN seconds for a gain of at least G dollars (the size of the user's three scalps, a fixed 0.20) or at least K typical minutes of that series' own curve? Fee not charged here: this counts how often the shape appears, it does not score a rule.
Kalshi's own quote tape only (rule: Kalshi feed). Quotes are put on a 1-second grid per ticker. Entries sit 60 s or more after the open, sales 60 s or more before the close, and the entry ask is at most 0.85, so a settlement drift is not counted. A typical minute = the series' root-mean-square one-second move of the yes mid times sqrt(60), over the whole period. Series are only labels on the table; the point is the spread between them and whether the curve measures (crossings, range) explain it.
    scripts/cloud_job.sh scripts/swing_frequency_by_series.py DAYS=3
Read-only. The job restores the newest nightly backup, so the period ends at that backup."""

import collections
import math
import os
import time

from kalshi_moonshot import config, db

os.nice(10)
DAYS = float(os.environ.get("DAYS", "3"))
G = float(os.environ.get("G", "0.20"))
K = float(os.environ.get("K", "3.0"))
SPAN = int(os.environ.get("SPAN", "360"))
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
end = float(tape.execute("SELECT max(ts) AS m FROM quotes").fetchone()["m"])
t0 = end - DAYS * 86400
print(f"period {time.strftime('%m-%d %H:%M', time.gmtime(t0))} to {time.strftime('%m-%d %H:%M', time.gmtime(end))} UTC, G={G}, K={K} typical minutes, span={SPAN}s")

tickers = [r["ticker"] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts > ? AND ticker LIKE 'KX%15M-%'", (t0,)).fetchall()]


def grid(rows, a, b):
    """last bid and ask for each second in [a, b), carried forward."""
    out_b = [None] * (b - a)
    out_a = [None] * (b - a)
    for ts, bid, ask in rows:
        k = int(ts) - a
        if 0 <= k < b - a and bid is not None and ask is not None:
            out_b[k], out_a[k] = float(bid), float(ask)
    lb = la = None
    for k in range(b - a):
        if out_b[k] is None:
            out_b[k], out_a[k] = lb, la
        else:
            lb, la = out_b[k], out_a[k]
    return out_b, out_a


def best_after(vals, span):
    """for each i, the highest value in (i, i+span] (sliding max)."""
    n = len(vals)
    res = [None] * n
    dq = collections.deque()
    for i in range(n - 1, -1, -1):
        hi = i + span
        while dq and dq[0] > hi:
            dq.popleft()
        res[i] = vals[dq[0]] if dq else None
        while dq and vals[dq[-1]] <= vals[i]:
            dq.pop()
        dq.append(i)
    return res


wins = []  # (series, gain_yes_side, gain_no_side, crossings, range)
sqsum = collections.defaultdict(float)
nsum = collections.defaultdict(int)
for tk in tickers:
    series = tk.split("-")[0]
    rows = [(r["ts"], r["bid"], r["ask"]) for r in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>? ORDER BY ts", (tk, t0 - 1)).fetchall()]
    if len(rows) < 200:
        continue
    first, last = int(rows[0][0]), int(rows[-1][0])
    close = (last // 900 + 1) * 900 if last % 900 else last
    a = close - 900
    if first > a + 120 or last < close - 30:
        continue  # not a whole window
    bids, asks = grid(rows, a, close)
    lo, hi = 60, 900 - 60
    if None in bids[:hi + SPAN if hi + SPAN < 900 else 900]:
        continue
    mid = [(x + y) / 2 for x, y in zip(bids, asks)]
    sqsum[series] += sum((mid[k + 1] - mid[k]) ** 2 for k in range(len(mid) - 1))
    nsum[series] += len(mid) - 1
    cap = 900 - 60  # sales stop 60 s before the close: later quotes are the settlement drift, never a sale
    yb = best_after(bids[:cap] + [-9.0] * (900 - cap), SPAN)  # yes side: buy at the ask, sell at the yes bid
    nb = best_after([1 - x for x in asks[:cap]] + [-9.0] * (900 - cap), SPAN)  # no side: buy at 1-bid, sell at 1-ask
    gy = max((yb[i] - asks[i] for i in range(lo, hi) if yb[i] is not None and yb[i] > -9 and asks[i] <= 0.85), default=0.0)
    gn = max((nb[i] - (1 - bids[i]) for i in range(lo, hi) if nb[i] is not None and nb[i] > -9 and (1 - bids[i]) <= 0.85), default=0.0)
    cross = sum(1 for k in range(1, len(mid)) if (mid[k] - 0.5) * (mid[k - 1] - 0.5) < 0)
    wins.append((series, gy, gn, cross, max(mid) - min(mid)))

unit = {sr: math.sqrt(sqsum[sr] / nsum[sr]) * math.sqrt(60) for sr in nsum if nsum[sr]}
by = collections.defaultdict(list)
for w in wins:
    by[w[0]].append(w)
print(f"{len(wins)} whole windows in {len(by)} series")
print(f"{'series':14} {'n':>5} {'unit':>6} {'G>=%.2f' % G:>8} {'K units':>8} {'both':>6} {'med gain':>9} {'cross':>6} {'range':>6}")
rows_out = []
for sr, L in by.items():
    n = len(L)
    u = unit[sr]
    gains = sorted(max(w[1], w[2]) for w in L)
    pg = sum(1 for g in gains if g >= G) / n
    pk = sum(1 for g in gains if g >= K * u) / n
    pb = sum(1 for g in gains if g >= G and g >= K * u) / n
    rows_out.append((pg, sr, n, u, pk, pb, gains[n // 2], sum(w[3] for w in L) / n, sum(w[4] for w in L) / n))
for pg, sr, n, u, pk, pb, med, cr, rg in sorted(rows_out, reverse=True):
    print(f"{sr:14} {n:5d} {u:6.3f} {pg:8.0%} {pk:8.0%} {pb:6.0%} {med:9.2f} {cr:6.1f} {rg:6.2f}")


def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    for k, i in enumerate(o):
        r[i] = k
    return r


def spearman(x, y):
    rx, ry = rank(x), rank(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((p - mx) * (q - my) for p, q in zip(rx, ry))
    den = math.sqrt(sum((p - mx) ** 2 for p in rx) * sum((q - my) ** 2 for q in ry))
    return num / den if den else float("nan")


if len(rows_out) >= 4:
    print("rank correlation across series of the G share with: mean crossings %.2f, mean range %.2f, typical minute %.2f" % (
        spearman([r[0] for r in rows_out], [r[7] for r in rows_out]),
        spearman([r[0] for r in rows_out], [r[8] for r in rows_out]),
        spearman([r[0] for r in rows_out], [r[3] for r in rows_out])))
print("DONE")
