"""Fade the projection: where our curve says a side is cheap, buy the other one (2026-11-08, user: "did we try comparing our prediction to Kalshi's and picking the opposite side?").

Question: the pure projection (no fit: the normal chance that the cushion is still above zero at the close, carrying a scale's drift forward over the seconds left) was far more bullish than Kalshi's ask on some moments, and those buys lost (scripts/curve_win_chance_walkforward.py: formula picks -0.10 per $1). So when the projection beats the ask of side S by at least D, does buying the OTHER side (at its ask, 1 - S's bid) pay after the fee, held to the result?
Setup: the moments of curve_time_left_walkforward.py (every settled crypto 15-minute window in the tape since 11-01, both sides, every 30 s, the model lane's price and spread bands). D in 0.05, 0.10, 0.15, 0.20 is a grid, not a choice after the fact; scales 13 and 34 minutes. First moment per market side, and every moment. Net per $1 after the full taker fee; win rate against the break-even rate of the price paid; two halves; by minutes into the window. The other side of a moment that is itself outside the model lane's price band is still scored at its own ask.
Not covered: queue, sales before the result, the same-moment hedge (buy both), the other side's spread beyond the displayed ask.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/fade_the_formula_study.py
"""

import os
import time

from kalshi_moonshot import config, curvemodel
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
W = {13: 29, 34: 30}
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def other(i):
    """(won, ask paid, net per $1) of the opposite side of moment i."""
    ask = 1.0 - meta[i][4]
    won = rows[i][2] <= 0  # this side lost, so the other won
    c = 1.0 / ask
    return won, ask, ((1 - ask) * c if won else -1.0) - fee(ask) * c


def show(label, idx):
    if not idx:
        print(f"   {label:34s} n=0")
        return
    sc = [other(i) for i in idx]
    n = len(idx)
    be = sum(a + fee(a) for _, a, _ in sc) / n
    ha = [x[2] for x, i in zip(sc, idx, strict=True) if meta[i][0] <= mid]
    hb = [x[2] for x, i in zip(sc, idx, strict=True) if meta[i][0] > mid]
    print(f"   {label:34s} n={n:5d} won {100 * sum(1 for x in sc if x[0]) / n:4.1f}% vs break-even {100 * be:4.1f}% | net per $1 {sum(x[2] for x in sc) / n:+.3f} | halves {sum(ha) / max(len(ha), 1):+.3f} (n={len(ha)}) / {sum(hb) / max(len(hb), 1):+.3f} (n={len(hb)})")


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


for scale in (13, 34):
    print(f"\nprojection at the {scale} minute scale: buy the other side when the projection beats this side's ask by D")
    for d in (0.05, 0.10, 0.15, 0.20):
        pick = [i for i in range(len(rows)) if rows[i][0][W[scale]] - rows[i][1] >= d]
        f = first(pick)
        show(f"D {d:.2f}, every moment", pick)
        show(f"D {d:.2f}, first per market side", f)
        for lo, hi in ((0, 240), (240, 480), (480, 900)):
            show(f"D {d:.2f}, first, {lo // 60}-{hi // 60} min in", [i for i in f if lo <= meta[i][1] < hi])
print("DONE")
