"""NEW BUY FLAG against the shuffle test (2026-11-23; user: "designing the new buy flag against this shuffle test"). Settings fixed before the run, not a sweep: on the raw spot, in units of its typical 10 s (sigma10, rms 10 s change over the hour before the open), a LEG runs from a pivot to its extreme and is confirmed when the spot comes off the extreme by at least 0.35 of the swing and at least sigma10; it is SIGNIFICANT when its size is at least Z=3 x sigma10 x sqrt(duration / 10 s) (duration at least 5 s): the price ratio, a leg judged against what a random walk moves in that time. The flag fires at a significant leg's confirmation (minutes 2 to 10) when at least M significant legs were confirmed in the last 180 s (M=1: a lone bump; M=2: back and forth) and the whole leg stayed on one side of the target (NO after a peak, YES after a dip). Gate (the null test, scripts/wave_null_test.py): the flag must fire MORE on the real path than on shuffled copies of it (one-second changes in random order); only then are its entries compared, on the same replayed fills as scripts/scalp_short_replay.py (taker at the ask, band 0.18 to 0.55), with the no-signal baseline: the bid rising one and two typical 10 s net of both fees within 30 s and 60 s, and the hold result per bet. Read-only; a lead only.
    .venv/bin/python scripts/wave_flag_lab.py [CACHE_DIR]"""

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



UNIT = 10

Z, T_FRAC, M_SET, WIN_LEGS = 3.0, 0.35, (1, 2), 180
SHUF = 2
rng = np.random.default_rng(5)


def flag_fires(spv, ia, strike, sig10, Ms=M_SET):
    """First fire time and side for each M in Ms: {M: (t, side) or None}. spv is a list of floats from second 0 (= 3,700 s before the open) through minute 10; ia the open's index."""
    out = {m: None for m in Ms}
    k0 = ia - 300  # five minutes of history before the open
    lo = hi = spv[k0]
    klo = khi = k0
    d = 0
    pk, pv = k0, spv[k0]
    ek, ev = k0, spv[k0]
    legs = []  # confirmed significant legs: confirmation second
    for k in range(k0 + 1, ia + ENTRY_TO + 1):
        v = spv[k]
        if d == 0:
            if v < lo:
                lo, klo = v, k
            if v > hi:
                hi, khi = v, k
            if v - lo >= sig10:
                d, pk, pv, ek, ev = 1, klo, lo, k, v
            elif hi - v >= sig10:
                d, pk, pv, ek, ev = -1, khi, hi, k, v
            continue
        if (v - ev) * d > 0:
            ek, ev = k, v
            continue
        draw = (ev - v) * d
        move = (ev - pv) * d
        if draw >= max(T_FRAC * move, sig10):
            dur = max(ek - pk, 5)
            z = move / (sig10 * math.sqrt(dur / 10.0))
            if z >= Z:
                legs.append(k)
                t = k - ia
                if ENTRY_FROM <= t <= ENTRY_TO:
                    recent = sum(1 for x in legs if k - x <= WIN_LEGS)
                    side = None
                    if d == 1 and pv > strike and v > strike:
                        side = "no"
                    elif d == -1 and pv < strike and v < strike:
                        side = "yes"
                    if side:
                        for m in Ms:
                            if out[m] is None and recent >= m:
                                out[m] = (t, side)
            pk, pv, ek, ev, d = ek, ev, k, v, -d
    return out


def sigma10(spv, ia):
    return math.sqrt(sum((spv[x] - spv[x - 10]) ** 2 for x in range(ia - 3600 + 10, ia, 10)) / max(1, len(range(ia - 3600 + 10, ia, 10))))


stats = {m: {"n": 0, "real": 0, "shuf": 0, "shuf_n": 0} for m in M_SET}
base = []
hits = {m: [] for m in M_SET}
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
    spc, prev = [], next(float(x) for x in sp if x is not None)
    for x in sp[: ia + ENTRY_TO + 1]:
        prev = float(x) if x is not None else prev  # a gap in the first 40 s (outside the hour the unit reads) carries the next value back, a later one the last value forward
        spc.append(prev)
    strike = float(w["strike"])
    sg = sigma10(spc, ia)
    if sg <= 0:
        continue
    n = close - a
    for t0 in range(max(qfirst, ENTRY_FROM), ENTRY_TO + 1, 30):
        if t0 + 70 >= n - 5:
            break
        for sd in ("yes", "no"):
            ak = asks[t0] if sd == "yes" else 1.0 - bids[t0]
            if LO <= ak <= HI:
                base.append(scalp(bids, asks, sd, ak, t0, n, sd == w["result"], w["kind"]))
    real = flag_fires(spc, ia, strike, sg)
    arr = np.array(spc, dtype=float)
    inc = np.diff(arr)
    shufs = []
    for _ in range(SHUF):
        sh = np.concatenate(([arr[0]], arr[0] + np.cumsum(rng.permutation(inc)))).tolist()
        shufs.append(flag_fires(sh, ia, strike - arr[ia] + sh[ia], sigma10(sh, ia)))
    for m in M_SET:
        stats[m]["n"] += 1
        stats[m]["real"] += real[m] is not None
        for sres in shufs:
            stats[m]["shuf_n"] += 1
            stats[m]["shuf"] += sres[m] is not None
        if real[m] is not None:
            t, side = real[m]
            ask = asks[t] if side == "yes" else 1.0 - bids[t]
            if LO <= ask <= HI and t + 70 < n - 5:
                hits[m].append(scalp(bids, asks, side, ask, t, n, side == w["result"], w["kind"]))


def rep(rows, label):
    ok = [r for r in rows if r["u"] > 0]
    m_ = len(ok)
    if not m_:
        return
    f = lambda k, x: sum(1 for r in ok if r[k] >= x * r["u"]) / m_
    print(f"{label:34s} n={m_:6d} 1u in 30 s {f('best30', 1):.2f} 60 s {f('best60', 1):.2f} | 2u in 30 s {f('best30', 2):.2f} 60 s {f('best60', 2):.2f} | win {sum(r['won'] for r in ok) / m_:.2f} hold {sum(r['H'] for r in ok) / m_:+.2f}/bet")


print("GATE: fire rate on the real path against shuffled copies (a first flag in minutes 2 to 10, no price band)")
for m in M_SET:
    s = stats[m]
    print(f"  M={m}: windows {s['n']}, real fires {s['real'] / s['n']:.3f}, shuffled fires {s['shuf'] / s['shuf_n']:.3f}, ratio real/shuffled {(s['real'] / s['n']) / max(1e-9, s['shuf'] / s['shuf_n']):.2f}")
print()
rep(base, "baseline, no signal")
for m in M_SET:
    rep(hits[m], f"flag M={m} (in band, taker at the ask)")
    for kd in ("crypto", "commodity"):
        rep([r for r in hits[m] if r["kind"] == kd], f"   {kd}")
print(f"\n{time.time() - t_start:.0f} s")
