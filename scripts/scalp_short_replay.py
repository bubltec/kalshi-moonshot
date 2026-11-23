"""SHORT-SCALE scalp replay (2026-11-23; user: "most of this happens under a minute, so we need to be more like typical 10s").

Same bets as scalp_bump_replay.py (the rebuilt detector, and a no-signal baseline every 30 s, on the study cache, crypto and commodity, band 0.18 to 0.55, entry minute 2 to 10, taker fill at the ask, fee on every leg), but every unit is the TYPICAL 10 SECONDS (env UNIT=20 for the typical 20 s the user also named): the rms 10-second change of the spot (for the bump) and of the bid's mid over the last 900 s (for the scalp bar). A scalp is the bid rising at least one and at least two typical 10-second moves net of both fees within 30 s and within 60 s of the buy. The bump (mine, fixed before the run): at scale N seconds (5, 10, 20, 30) the spot's high is at least 3 x (typical 10 s x sqrt(N/10)) above the low of the N seconds before it, the spot has come off the high by at least half of that typical move and no more than half the rise, the whole bump on one side of the target (NO on a peak, YES on a dip). Also the spread paid at the buy, in typical 10 s, so the cost of the scalp can be told apart from the signal: hit rates by spread third (cuts from the baseline). Read-only; a lead only.
    .venv/bin/python scripts/scalp_short_replay.py [CACHE_DIR]"""

import math
import os
import numpy as np
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


def fee1(p):
    return 0.07 * p * (1 - p)


def scalp(bids, asks, side, ask, t, n, won, kind):
    """The bid path after a buy at `ask` at second t, in the bid's typical 10 seconds (rms 10 s change of the mid over the last 900 s): the best net gain per contract within 30 s and 60 s, the seconds to the first gain of one and two typical 10 s, the spread paid in those units, and the hold net."""
    lo = max(0, t - 900)
    mid = [0.5 * (bids[s] + asks[s]) for s in range(lo, t + 1)]
    ch = [mid[i + UNIT] - mid[i] for i in range(0, len(mid) - UNIT, 5)]
    u = math.sqrt(sum(c * c for c in ch) / len(ch)) if len(ch) >= 30 else 0.0
    basis = ask + fee1(ask)
    best, t1, t2 = {30: -9.0, 60: -9.0}, None, None
    for s in range(t + 1, min(n - 5, t + 61)):
        x = (bids[s] if side == "yes" else 1.0 - asks[s])
        net = x - fee1(x) - basis
        for h in (30, 60):
            if s - t <= h:
                best[h] = max(best[h], net)
        if u > 0 and t1 is None and net >= u:
            t1 = s - t
        if u > 0 and t2 is None and net >= 2 * u:
            t2 = s - t
    return {"u": u, "best30": best[30], "best60": best[60], "t1": t1, "t2": t2, "H": hold_net(ask, won), "won": won, "kind": kind, "ask": ask, "spr": ((asks[t] - bids[t]) / u) if u > 0 else 9.9}


def eff(A, k, span):
    seg = A[k - span : k + 1]
    if None in seg:
        return 0.0
    return abs(seg[-1] - seg[0]) / max(1e-12, sum(abs(y - x) for x, y in zip(seg, seg[1:], strict=False)))


def xcount(sp, A, k, span):
    d = [sp[i] - A[i] for i in range(k - span, k + 1) if sp[i] is not None and A[i] is not None]
    return sum(1 for x, y in zip(d, d[1:], strict=False) if x * y < 0)


base = []
bets = []
UNIT = int(os.environ.get("UNIT", "10"))  # the typical move is over this many seconds (10 or 20)
SCALES = (5, 10, 20, 30)
BZ, BT = 3.0, 0.5
bump = {N: [] for N in SCALES}
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
    n0 = close - a
    for t0 in range(max(qfirst, ENTRY_FROM), ENTRY_TO + 1, 30):
        if t0 + 70 >= n0 - 5:
            break
        for sd in ("yes", "no"):
            ak = asks[t0] if sd == "yes" else 1.0 - bids[t0]
            if not (LO <= ak <= HI):
                continue
            base.append(scalp(bids, asks, sd, ak, t0, n0, sd == w["result"], w["kind"]))
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
        sc = scalp(bids, asks, side, ask, t, n, won, w["kind"])
        bets.append({**sc, "u": u, "bid": bid_s, "mid": [0.5 * (bids[x] + asks[x]) for x in range(n)], "t": t, "n": n, "close": close, "side": side, "ask": ask, "won": won, "H": hold_net(ask, won), "range_u": (max(win) - min(win)) / u, "cross": cross, "swings": swings(win, 0.5 * u), "eff_b": abs(win[-1] - win[0]) / max(1e-9, sum(abs(y - x) for x, y in zip(win, win[1:], strict=False))), "eff_s": abs(sp[k] - sp[k - 300]) / max(1e-9, sum(abs(y - x) for x, y in zip(sp[k - 300 : k + 1], sp[k - 299 : k + 1], strict=False))), "drift_s": (sp[k] - sp[k - 300]) / ua * (1 if side == "yes" else -1), "kind": w["kind"], "eff5": eff(A, k, 300), "eff10": eff(A, k, 600), "cross5": xcount(sp, A, k, 300), "reach": abs(A[k] - strike) / ua, "run10": (A[k] - A[k - 600]) / ua * (1 if side == "no" else -1), "u": u, "t_min": t / 60.0})
        break

    ur = math.sqrt(sum((sp[x] - sp[x - UNIT]) ** 2 for x in range(ia - 3600 + UNIT, ia, 10)) / max(1, len(range(ia - 3600 + UNIT, ia, 10))))
    spa = np.array([x if x is not None else np.nan for x in sp], dtype=float)
    if ur > 0:
        for N in SCALES:
            un = ur * math.sqrt(N / float(UNIT))
            for k in range(ia + max(qfirst, ENTRY_FROM), ia + ENTRY_TO + 1):
                t = k - ia
                win = spa[k - N + 1 : k + 1]
                if np.isnan(win).any():
                    continue
                side = None
                ih = int(len(win) - 1 - np.argmax(win[::-1]))
                hi = win[ih]
                kh = k - N + 1 + ih
                lo = np.nanmin(spa[max(0, kh - N) : kh + 1])
                if (hi - lo) / un >= BZ and BT * un <= hi - spa[k] <= 0.5 * (hi - lo) and lo > strike and spa[k] > strike:
                    side = "no"
                else:
                    il = int(len(win) - 1 - np.argmin(win[::-1]))
                    lw = win[il]
                    kl = k - N + 1 + il
                    top = np.nanmax(spa[max(0, kl - N) : kl + 1])
                    if (top - lw) / un >= BZ and BT * un <= spa[k] - lw <= 0.5 * (top - lw) and top < strike and spa[k] < strike:
                        side = "yes"
                if side is None:
                    continue
                ask = asks[t] if side == "yes" else 1.0 - bids[t]
                if not (LO <= ask <= HI):
                    continue
                n = close - a
                if t + 70 >= n - 5:
                    break
                bump[N].append({**scalp(bids, asks, side, ask, t, n, side == w["result"], w["kind"]), "t": t})
                break


def rep(rows, label):
    ok = [r for r in rows if r["u"] > 0]
    m = len(ok)
    if not m:
        return
    f = lambda k, x: sum(1 for r in ok if r[k] >= x * r["u"]) / m
    ts = sorted(r["t1"] for r in ok if r["t1"] is not None)
    med = ts[len(ts) // 2] if ts else float("nan")
    print(f"{label:30s} n={m:6d} 1u in 30 s {f('best30', 1):.2f} 60 s {f('best60', 1):.2f} | 2u in 30 s {f('best30', 2):.2f} 60 s {f('best60', 2):.2f} | median s to 1u {med:4.0f} | spread {sum(r['spr'] for r in ok) / m:.2f}u | win {sum(r['won'] for r in ok) / m:.2f} hold {sum(r['H'] for r in ok) / m:+.2f}/bet")


def by_spread(rows, label, cuts):
    out = []
    for lo_, hi_ in ((0, cuts[0]), (cuts[0], cuts[1]), (cuts[1], 99)):
        rr = [r for r in rows if r["u"] > 0 and lo_ < r["spr"] <= hi_ or (lo_ == 0 and r["u"] > 0 and r["spr"] <= hi_ and r["spr"] >= 0 and lo_ == 0)]
        rr = [r for r in rr if lo_ <= r["spr"] <= hi_]
        out.append(f"{sum(1 for r in rr if r['best30'] >= r['u']) / max(1, len(rr)):.2f} (n={len(rr)})")
    print(f"{label:30s} 1u in 30 s by spread third: low {out[0]}  mid {out[1]}  high {out[2]}")


print(f"{len(bets)} detector bets, {len(base)} baseline entries (no signal)")
groups = [("old detector (60 s average)", bets), ("baseline, no signal", base)] + [(f"bump {N} s", bump[N]) for N in SCALES]
for lab, rows in groups:
    rep(rows, lab)
    for kd in ("crypto", "commodity"):
        rep([r for r in rows if r["kind"] == kd], f"   {kd}")
sp_all = sorted(r["spr"] for r in base if r["u"] > 0)
cuts = (sp_all[len(sp_all) // 3], sp_all[2 * len(sp_all) // 3])
print(f"\nspread cuts (typical 10 s, from the baseline): {cuts[0]:.2f} / {cuts[1]:.2f}")
for lab, rows in groups:
    by_spread(rows, lab, cuts)
print(f"\n{time.time() - t_start:.0f} s")
