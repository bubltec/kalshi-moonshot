"""Does a wave-top buy that comes after a BACK-AND-FORTH curve pay, and one after a one-way run not? (2026-11-23; user, on WTI 04:00, NATGAS 04:00, PLATINUM 04:15, BTC 05:00, HYPE 05:45: "didn't exhibit the wave behavior", "a flat/trend not a wave pattern", "bought in a trough, not a dip"; on 11-21: "you need to watch it go back and forth a little".)

Copied from wave_curve_measures_study.py. On the same bets (the rebuilt detector wavetop.signal_at on the study cache, crypto AND commodity, band 0.18 to 0.55, entry minute 2 to 10, taker fill at the ask, 25 contracts, held to settlement, fee on every leg; the lane's staircase at its live settings k 1.0 p 1.0, cushion off), the measures read at the buy from the spot's own path, in its own units (CLAUDE.md rule 1; the kind is a label in the report, nothing splits on it):
  eff5, eff10   path efficiency of the 60 s average over the 5 and 10 minutes before the buy (net move / distance travelled; 1 = a straight line);
  cross5        how many times the raw spot crossed its 60 s average in the 5 minutes before the buy;
  reach         the average's distance from the target at the buy, in the average's typical minutes (how far the bet must come back to cross it);
  run10         the average's net move over the 10 minutes before the buy toward the bet's side, in typical minutes (a run that has gone one way).
Thirds are this sample's cuts, said after seeing it. A lead only; read-only.
    .venv/bin/python scripts/wave_backforth_study.py [CACHE_DIR]"""

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
cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else "job-results/20261123-022002-76512")
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


def eff(A, k, span):
    seg = A[k - span : k + 1]
    if None in seg:
        return 0.0
    return abs(seg[-1] - seg[0]) / max(1e-12, sum(abs(y - x) for x, y in zip(seg, seg[1:], strict=False)))


def xcount(sp, A, k, span):
    d = [sp[i] - A[i] for i in range(k - span, k + 1) if sp[i] is not None and A[i] is not None]
    return sum(1 for x, y in zip(d, d[1:], strict=False) if x * y < 0)


bets = []
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["result"] not in ("yes", "no") or w["strike"] is None:
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
        bets.append({"u": u, "bid": bid_s, "mid": [0.5 * (bids[x] + asks[x]) for x in range(n)], "t": t, "n": n, "close": close, "side": side, "ask": ask, "won": won, "H": hold_net(ask, won), "range_u": (max(win) - min(win)) / u, "cross": cross, "swings": swings(win, 0.5 * u), "eff_b": abs(win[-1] - win[0]) / max(1e-9, sum(abs(y - x) for x, y in zip(win, win[1:], strict=False))), "eff_s": abs(sp[k] - sp[k - 300]) / max(1e-9, sum(abs(y - x) for x, y in zip(sp[k - 300 : k + 1], sp[k - 299 : k + 1], strict=False))), "drift_s": (sp[k] - sp[k - 300]) / ua * (1 if side == "yes" else -1), "kind": w["kind"], "eff5": eff(A, k, 300), "eff10": eff(A, k, 600), "cross5": xcount(sp, A, k, 300), "reach": abs(A[k] - strike) / ua, "run10": (A[k] - A[k - 600]) / ua * (1 if side == "no" else -1), "u": u, "t_min": t / 60.0})
        break


def fee1(p):
    return 0.07 * p * (1 - p)


def staircase(b, k=0.25, p=0.5, from_t=0):
    """The lane's staircase with its defaults (cushion off, no high-zone rule): the sale's net dollars, or the hold result."""
    ask, bid, mid, t, n = b["ask"], b["bid"], b["mid"], b["t"], b["n"]
    basis = ask + fee1(ask)
    d, hi, lo, last, best = 0, 0.0, 0.0, 0.0, -9.0
    for s_ in range(t + 1, n - 3):
        x = bid[s_]
        net = x - fee1(x) - basis
        best = max(best, net)
        w = mid[max(0, s_ - 900) : s_ + 1]
        ch = [y - z for z, y in zip(w, w[1:], strict=False)]
        u = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(60.0) if len(ch) >= 30 else 0.0
        rev = max(0.01, k * u * (max(n - s_, 60.0) / 60.0) ** p) if u > 0 else 0.05
        if hi <= 0:
            hi = lo = x
        if d >= 0:
            hi = max(hi, x)
            if hi - x >= rev - 1e-9:
                d, lo = -1, x
        if d < 0:
            lo = min(lo, x)
            if x - lo >= rev - 1e-9:
                last, d, hi = lo, 1, x
        if s_ >= b["t"] + from_t and best > 0 and d < 0 and last > 0 and x < last - 1e-9:
            return QTY * x - fee(x) - (QTY * ask + fee(ask))
    return b["H"]


def stop(b, m):
    """Sell at the bid the first second after the shape is read (120 s) that it is m of the bid's typical minutes under the price paid."""
    ask, bid, t, n, u = b["ask"], b["bid"], b["t"], b["n"], b["u"]
    for s_ in range(t + LOOK, n - 3):
        if bid[s_] <= ask - m * u:
            x = bid[s_]
            return QTY * x - fee(x) - (QTY * ask + fee(ask))
    return b["H"]


def stats(rows):
    n = len(rows)
    xs = [r["H"] for r in rows]
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(n - 1, 1))
    return f"n={n:4d} win {sum(r['won'] for r in rows) / n:.2f} mean ask {sum(r['ask'] for r in rows) / n:.3f} hold total {sum(xs):8.2f} per bet {m:+6.2f} +- {sd / math.sqrt(n):.2f}"



print(f"{len(bets)} bets; all: {stats(bets)}")
for b in bets:
    b["S"] = staircase(b, 1.0, 1.0, 0)
print(f"staircase total {sum(b['S'] for b in bets):.2f} vs hold {sum(b['H'] for b in bets):.2f}")
MEAS = (("range_u", "bid range, first 120 s (bid typical minutes)"), ("eff_b", "bid path efficiency, first 120 s (1 = straight)"), ("cross", "crossings of the price paid, first 120 s"), ("eff_s", "spot path efficiency, 5 min before the buy"), ("drift_s", "spot drift toward the bet, 5 min before the buy (spot typical minutes)"), ("eff5", "average path efficiency, 5 min before the buy (1 = straight)"), ("eff10", "average path efficiency, 10 min before the buy"), ("cross5", "spot crossings of its 60 s average, 5 min before the buy"), ("reach", "average distance from the target at the buy (average typical minutes)"), ("run10", "average run in the wave direction over 10 min before the buy (typical minutes)"), ("u", "the bid's typical minute"), ("t_min", "minutes into the market at the buy"))
for key, label in MEAS:
    xs = sorted(b[key] for b in bets)
    c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    print(f"\n{label}: cuts {c1:.3f} / {c2:.3f}")
    for lab, test in (("low", lambda v: v <= c1), ("mid", lambda v: c1 < v <= c2), ("high", lambda v: v > c2)):
        rr = [b for b in bets if test(b[key])]
        if not rr:
            continue
        n = len(rr)
        h = sum(b["H"] for b in rr) / n
        st = sum(b["S"] for b in rr) / n
        print(f"  {lab:5s} n={n:4d} commodity share {sum(b['kind'] == 'commodity' for b in rr) / n:.2f} win {sum(b['won'] for b in rr) / n:.2f} hold {h:+6.2f}/bet staircase {st:+6.2f}/bet (staircase minus hold {st - h:+6.2f})")
print(f"\n{time.time() - t_start:.0f} s")
