"""Copied from wave_bid_shape_study.py (2026-11-23); the tail asks the TURN question: was the side's bid already rising in the 30 s before the buy, in the bid's typical minutes, and does hold pay differently by that? A lead only (read-only).

Original question: does the SHAPE of a wave-top bet's own bid path in its first two minutes say whether holding it pays? (2026-11-22; user, on the BNB 15:15 NO bet that the salvage sold at +9% and that then won: "maybe we need a different sale to qualify the BNB behavior, because that's a different kind of wave".)

BNB's bid ran 0.24 -> 0.03 -> 0.32 -> 0.23 -> 1.00: big swings back and forth across its buy price. The question: on the same bets (the lane's detector, band 0.18 to 0.55, entry minute 2 to 10, taker fill at the ask, 25 contracts, held to settlement, fee on every leg; the study cache, crypto only, 11-09 to 11-21), is the hold result different for bets whose bid path in the first 120 s after the buy was a whipsaw than for ones that went one way? Shape measures, all in the bet's own units (CLAUDE.md rule 1: classify by curve, never by asset), read at 120 s after the buy so a rule could use them:
  range_u   the bid's high minus low over the 120 s, in the bid's typical minute (rms one-second change of the mid over the last 900 s, times sqrt(60));
  crossings how many times the bid crossed the price paid in those 120 s;
  swings    the confirmed swings of the bid at a reversal of half a typical minute.
Split into thirds (the cut points are this sample's, said after seeing it). A lead only: it shows whether a shape claim is worth a forward test.
    .venv/bin/python scripts/wave_bid_shape_study.py [CACHE_DIR]
Read-only."""

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc  # noqa: E402

from kalshi_moonshot import wavetop as wt  # noqa: E402

LO, HI, QTY = 0.18, 0.55, 25
ENTRY_FROM, ENTRY_TO, LOOK = 120, 600, 120
cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else "job-results/20261121-154853-21423")
t_start = time.time()


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


def hold_net(ask, won):
    cost = QTY * ask + fee(ask)
    return (QTY - cost) if won else -cost


def swings(path, rev):
    d, hi, lo, n = 0, path[0], path[0], 0
    for x in path:
        if d >= 0:
            hi = max(hi, x)
            if hi - x >= rev:
                d, lo, n = -1, x, n + 1
        if d < 0:
            lo = min(lo, x)
            if x - lo >= rev:
                d, hi, n = 1, x, n + 1
    return n


bets = []
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["kind"] != "crypto" or w["result"] not in ("yes", "no") or w["strike"] is None:
        continue
    close = int(w["close"])
    a = close - 900
    ia = 3700
    T, V = cache.ticks(w["index"])
    sp = wt.grid(T, V, a - 3700, close)
    if None in sp[ia - 3660 : ia + ENTRY_TO + 1]:
        continue
    qt, qb, qa = cache.quotes(w)
    if len(qt) < 100:
        continue
    bids, asks = wt.grid(qt, qb, a, close), wt.grid(qt, qa, a, close)
    qfirst = max(0, int(qt[0]) - a + 1)
    if qfirst > ENTRY_TO or None in bids[qfirst:] or None in asks[qfirst:]:
        continue
    bids = [bids[qfirst]] * qfirst + bids[qfirst:]
    asks = [asks[qfirst]] * qfirst + asks[qfirst:]
    A = wt.average(sp)
    ua = wt.typical_minute(A, ia)
    if ua <= 0:
        continue
    strike = float(w["strike"])
    z = wt.Zig(ia - wt.LOOK, A[ia - wt.LOOK], ua, 0.0, wt.LEG_A)
    for k in range(ia - wt.LOOK + 1, ia + ENTRY_TO + 1):
        z.step(k, A[k])
        t = k - ia
        if t < max(qfirst, ENTRY_FROM):
            continue
        wv, _ = wt.signal_at(z, A[k], sp[k], strike, ua, sp[ia])
        if wv is None:
            continue
        side = wv.side
        ask = asks[t] if side == "yes" else 1.0 - bids[t]
        if not (LO <= ask <= HI):
            continue
        n = close - a
        if t + LOOK >= n - 5:
            break
        bid_s = [(bids[s] if side == "yes" else 1.0 - asks[s]) for s in range(n)]
        mid = [0.5 * (bids[s] + asks[s]) for s in range(max(0, t + LOOK - 900), t + LOOK + 1)]
        ch = [y - x for x, y in zip(mid, mid[1:], strict=False)]
        u = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(60.0) if len(ch) >= 30 else 0.0
        if u <= 0:
            break
        win = bid_s[t : t + LOOK + 1]
        cross = sum(1 for x, y in zip(win, win[1:], strict=False) if (x - ask) * (y - ask) < 0)
        won = side == w["result"]
        bets.append({"u": u, "bid": bid_s, "mid": [0.5 * (bids[x] + asks[x]) for x in range(n)], "t": t, "n": n, "close": close, "side": side, "ask": ask, "won": won, "H": hold_net(ask, won), "range_u": (max(win) - min(win)) / u, "cross": cross, "swings": swings(win, 0.5 * u)})
        break


def stats(rows):
    n = len(rows)
    xs = [r["H"] for r in rows]
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(n - 1, 1))
    return f"n={n:4d} win {sum(r['won'] for r in rows) / n:.2f} mean ask {sum(r['ask'] for r in rows) / n:.3f} hold total {sum(xs):8.2f} per bet {m:+6.2f} +- {sd / math.sqrt(n):.2f}"



LAG = 30


def rise_u(b):
    t, bid, mid = b["t"], b["bid"], b["mid"]
    w = mid[max(0, t - 900) : t + 1]
    ch = [y - x for x, y in zip(w, w[1:], strict=False)]
    u = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(60.0) if len(ch) >= 30 else 0.0
    return (bid[t] - bid[max(0, t - LAG)]) / u if u > 0 else None


rows = [dict(b, r=rise_u(b)) for b in bets]
rows = [b for b in rows if b["r"] is not None]
print(f"{len(rows)} bets with a reading; all: {stats(rows)}")
xs = sorted(b["r"] for b in rows)
print("\nTHE TURN: the side's bid change over the 30 s before the buy, in the bid's typical minutes (positive = already turning up). Hold to settlement.")
for cut in (-0.5, -0.25, 0.0, 0.25, 0.5):
    lo = [b for b in rows if b["r"] <= cut]
    hi = [b for b in rows if b["r"] > cut]
    print(f"  cut {cut:+.2f}  at or under: {stats(lo)}\n             over      : {stats(hi)}")
c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
print(f"\nthirds (cuts {c1:+.2f}, {c2:+.2f}, from this sample):")
for lab, test in (("lowest", lambda v: v <= c1), ("middle", lambda v: c1 < v <= c2), ("highest", lambda v: v > c2)):
    rr = [b for b in rows if test(b["r"])]
    print(f"  {lab:8s} {stats(rr)}")
    for side in ("yes", "no"):
        r2 = [b for b in rr if b["side"] == side]
        print(f"      {side:3s} {stats(r2)}")
print(f"\n{time.time() - t_start:.0f} s")
