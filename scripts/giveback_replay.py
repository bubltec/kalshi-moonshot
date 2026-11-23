"""GIVE-BACK replay (2026-11-23; user: "yes" to a replay of the give-back sale, for the bets that were ahead once and then slid; live: 31 of 116 wave_top bets since 11-22 18:00 UTC reached about +0.285 a contract and ended about even, 27 were never ahead and none won).

Same bets as the other replays (the rebuilt detector on the study cache, crypto and commodity, band 0.18 to 0.55, entry minute 2 to 10, taker fill at the ask, 25 contracts, held to settlement, fee on every leg; the sale at the bid, from the recorded quotes, so the paper sale-price bug does not touch it). One stated rule, set before the run, not a sweep (CLAUDE.md; user feedback): the swing rev = k x the bid's typical minute (rms one-second change of the mid over the last 900 s x sqrt(60); no unit, no sale) x (max(left, 60) / 60)^p with k 1.0 and p 1.0 (the lane's settings), cushion off (no spot reading here).
  live staircase  sells the first time the bid falls under its last swing low, once the bet has been ahead (the code as merged 2026-11-23: best net > 0 and (net > 0 or best net > 1 swing));
  give-back       once the best net after both fees is over one swing, sells the first time the bid falls one swing under its running high, at a gain or a small loss;
  combined        whichever of the two fires first.
Reported: total dollars against holding, how many bets each beat or lost to holding, and by class (never ahead; ahead once), by thirds of the bid's typical minute at the buy and by kind (kind is a label, nothing splits on it). Thirds are this sample's cuts. A lead only; read-only.
    .venv/bin/python scripts/giveback_replay.py [CACHE_DIR]"""

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


import numpy as np  # noqa: E402


def fee1(p):
    return 0.07 * p * (1 - p)


def unit_series(mid):
    """The bid's typical minute at every second: rms one-second change of the mid over the last 900 s x sqrt(60); 0 until 30 changes are behind it."""
    m = np.asarray(mid, dtype=float)
    d2 = np.concatenate(([0.0], np.cumsum(np.diff(m) ** 2)))  # d2[i] = sum of squared changes up to second i
    u = np.zeros(len(m))
    for s_ in range(len(m)):
        lo = max(0, s_ - 900)
        cnt = s_ - lo
        if cnt >= 30:
            u[s_] = math.sqrt((d2[s_] - d2[lo]) / cnt) * math.sqrt(60.0)
    return u


def run(b, k=1.0, p=1.0, arm=1.0):
    """(live staircase, give-back, combined): each a (net dollars, second sold or None). The staircase is the lane's code as merged; the give-back is described above."""
    ask, bid, t, n, U = b["ask"], b["bid"], b["t"], b["n"], b["U"]
    basis = ask + fee1(ask)
    cost = QTY * ask + fee(ask)
    hold = b["H"]
    best, hi_bid = -9.0, 0.0
    d, hi, lo, last = 0, 0.0, 0.0, 0.0  # the staircase's state
    out_s = out_g = None
    for s_ in range(t + 1, n - 3):
        x = bid[s_]
        net = x - fee1(x) - basis
        best = max(best, net)
        hi_bid = max(hi_bid, x)
        u = U[s_]
        if u <= 0:
            continue  # no reading of the bid's own typical minute: no sale, as the code now does
        rev = max(0.01, k * u * (max(n - s_, 60.0) / 60.0) ** p)
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
        if out_s is None and best > 0 and (net > 0 or best > arm * rev) and d < 0 and last > 0 and x < last - 1e-9:
            out_s = s_
        if out_g is None and best > arm * rev and x <= hi_bid - rev - 1e-9:
            out_g = s_
        if out_s is not None and out_g is not None:
            break
    pay = lambda s_: QTY * bid[s_] - fee(bid[s_]) - cost
    st = pay(out_s) if out_s is not None else hold
    gb = pay(out_g) if out_g is not None else hold
    firsts = [x for x in (out_s, out_g) if x is not None]
    cb = pay(min(firsts)) if firsts else hold
    return (st, out_s), (gb, out_g), (cb, min(firsts) if firsts else None)


def best_net(b):
    ask, bid, t, n = b["ask"], b["bid"], b["t"], b["n"]
    basis = ask + fee1(ask)
    return max(bid[s_] - fee1(bid[s_]) - basis for s_ in range(t + 1, n - 3))


print(f"{len(bets)} bets")
for b in bets:
    b["U"] = unit_series(b["mid"])
    b["ut"] = float(b["U"][b["t"]])
    (b["S"], b["Ss"]), (b["G"], b["Gs"]), (b["C"], b["Cs"]) = run(b)
    b["best"] = best_net(b)
print(f"mean bid unit at the buy {sum(b['ut'] for b in bets) / len(bets):.4f} (a typical minute of a 0 to 1 price; a mix-up of units would show here)")


def line(rows, label):
    n = len(rows)
    if not n:
        return
    f = lambda key: sum(r[key] for r in rows)
    sold = lambda key: sum(1 for r in rows if r[key] is not None)
    beat = lambda key: sum(1 for r in rows if r[key] > r["H"] + 1e-9)
    lost = lambda key: sum(1 for r in rows if r[key] < r["H"] - 1e-9)
    print(f"{label:34s} n={n:5d} hold {f('H'):8.0f} | staircase {f('S'):8.0f} (sold {sold('Ss')}, beat {beat('S')}, lost {lost('S')}) | give-back {f('G'):8.0f} (sold {sold('Gs')}, beat {beat('G')}, lost {lost('G')}) | combined {f('C'):8.0f} (sold {sold('Cs')}, beat {beat('C')}, lost {lost('C')})")


line(bets, "all bets")
line([b for b in bets if b["kind"] == "crypto"], "   crypto")
line([b for b in bets if b["kind"] == "commodity"], "   commodity")
print()
line([b for b in bets if b["best"] <= 0], "never ahead (best net <= 0)")
line([b for b in bets if b["best"] > 0 and not b["won"]], "ahead once, then lost")
line([b for b in bets if b["best"] > 0 and b["won"]], "ahead once, then won")
print()
xs = sorted(b["ut"] for b in bets)
c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
print(f"thirds of the bid's typical minute at the buy: cuts {c1:.4f} / {c2:.4f}")
for lab, test in (("low", lambda v: v <= c1), ("mid", lambda v: c1 < v <= c2), ("high", lambda v: v > c2)):
    line([b for b in bets if test(b["ut"])], f"   {lab}")
print(f"\n{time.time() - t_start:.0f} s")
