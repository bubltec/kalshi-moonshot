"""CURVE-SHAPE SPLIT of the new buy flag (2026-11-23; user: "yes" to the split of M=1 by curve shape; CLAUDE.md rule 4: a rule needs a positive result in each curve shape it claims). The flag is the one in scripts/wave_flag_lab.py (fixed before that run). For its entries (taker at the ask, band 0.18 to 0.55, held to settlement, fee on every leg), hold per bet by thirds of readings taken AT the buy, none after it: the spot cushion (the 60 s average's distance past the target on the bet's side, in the spot's typical minutes: the board's `spot`), the board's `p_lose` (chance the spot's average ends on the wrong side), the bid's typical 10 s, the spot's path efficiency over the 5 minutes before the buy (1 = straight), the minutes into the market, the flagged leg's significance (z) and the number of significant legs in the last 180 s. Thirds are this sample's cuts, said after seeing it. Kind is shown as a label, nothing splits on it. Read-only; a lead only.
    .venv/bin/python scripts/wave_flag_shapes.py [CACHE_DIR]"""

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
                                out[m] = (t, side, z, recent)
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
            t, side, zq, nlegs = real[m]
            ask = asks[t] if side == "yes" else 1.0 - bids[t]
            if LO <= ask <= HI and t + 70 < n - 5:
                r = scalp(bids, asks, side, ask, t, n, side == w["result"], w["kind"])
                k = ia + t
                avg = sum(spc[k - 59 : k + 1]) / 60.0
                un = math.sqrt(sum((spc[x] - spc[x - 1]) ** 2 for x in range(k - 599, k + 1)) / 600.0) * math.sqrt(60.0)
                cush = ((avg - strike) if side == "yes" else (strike - avg)) / un if un > 0 else 0.0
                seg = spc[k - 300 : k + 1]
                path = sum(abs(y - x) for x, y in zip(seg, seg[1:]))
                r.update({"cushion": cush, "p_lose": 0.5 * (1.0 + math.erf(-cush / math.sqrt(max(n - t, 1.0) / 60.0) / math.sqrt(2.0))), "bid_u": r["u"], "spot_eff": abs(seg[-1] - seg[0]) / path if path > 0 else 0.0, "minute": t / 60.0, "leg_z": zq, "legs_180": nlegs})
                hits[m].append(r)


def rep(rows, label):
    ok = [r for r in rows if r["u"] > 0]
    m_ = len(ok)
    if not m_:
        return
    f = lambda k, x: sum(1 for r in ok if r[k] >= x * r["u"]) / m_
    print(f"{label:34s} n={m_:6d} 1u in 30 s {f('best30', 1):.2f} 60 s {f('best60', 1):.2f} | 2u in 30 s {f('best30', 2):.2f} 60 s {f('best60', 2):.2f} | win {sum(r['won'] for r in ok) / m_:.2f} hold {sum(r['H'] for r in ok) / m_:+.2f}/bet")


FEATS = (("cushion", "spot cushion at the buy (spot typical minutes, + = the spot backs the bet)"), ("p_lose", "board p_lose at the buy"), ("bid_u", "the bid's typical 10 s at the buy"), ("spot_eff", "spot path efficiency, 5 min before the buy"), ("minute", "minutes into the market"), ("leg_z", "the flagged leg's significance z"), ("legs_180", "significant legs in the last 180 s"))


def hold_line(rows, label):
    nn = len(rows)
    if nn < 5:
        print(f"   {label:6s} n={nn}")
        return
    xs = [r["H"] for r in rows]
    mu = sum(xs) / nn
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (nn - 1))
    print(f"   {label:6s} n={nn:5d} commodity share {sum(r['kind'] == 'commodity' for r in rows) / nn:.2f} win {sum(r['won'] for r in rows) / nn:.2f} hold {mu:+.2f}/bet +- {sd / math.sqrt(nn):.2f}  total {sum(xs):8.0f}")


for m in M_SET:
    rows = [r for r in hits[m] if r["u"] > 0]
    print(f"\n=== flag M={m}: {len(rows)} entries")
    hold_line(rows, "all")
    for key, label in FEATS:
        xs = sorted(r[key] for r in rows)
        c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
        print(f"{label}: cuts {c1:.3f} / {c2:.3f}")
        for lab, test in (("low", lambda v: v <= c1), ("mid", lambda v: c1 < v <= c2), ("high", lambda v: v > c2)):
            hold_line([r for r in rows if test(r[key])], lab)
print(f"\n{time.time() - t_start:.0f} s")
