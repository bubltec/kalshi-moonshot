"""GIVE-BACK replay with the fact board's cushion (2026-11-23; user: "it just means we're missing some flag that lets winners pass", "use the fact board").

The first give-back replay (scripts/giveback_replay.py) ran with the spot cushion OFF and lost to holding (-1,305 against -67): it cut 694 winners that were ahead once. The fact board already has the flag that lets a bet the spot backs pass: `p_lose` (the chance the spot's average ends on the wrong side: its cushion over a random walk's move in the time left, wavetop.p_lose) and `cushion_factor` = 2 x (1 - p_lose), which scales the swing (the lane's wave_cushion, on in the lane file). Mirrored here from the board's `spot` reader (wavetop.spot_read): cushion = the 60 s average's distance past the strike on the bet's side, unit = the spot's rms one-second change over the last 600 s x sqrt(60); no reading means a factor of 1, as the board does.
Same bets and the same single stated rule as before (swing = k 1.0 x the bid's typical minute x (time left / 60)^1.0 x the cushion factor; the staircase as merged; the give-back one swing over the best gain, sold one swing under the running high), run with the cushion OFF and ON. Then, for the bets the cushion-off give-back sold, the p_lose it read at the sale, in thirds (cuts are this sample's), against how many of those bets won and the dollars it saved or cost: the question is whether p_lose separates the winners it cut from the losers it saved. Read-only; a lead only.
    .venv/bin/python scripts/giveback_cushion_replay.py [CACHE_DIR]"""

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
        bets.append({"u": u, "bid": bid_s, "mid": [0.5 * (bids[x] + asks[x]) for x in range(n)], "t": t, "n": n, "close": close, "side": side, "ask": ask, "won": won, "H": hold_net(ask, won), "range_u": (max(win) - min(win)) / u, "cross": cross, "swings": swings(win, 0.5 * u), "eff_b": abs(win[-1] - win[0]) / max(1e-9, sum(abs(y - x) for x, y in zip(win, win[1:], strict=False))), "eff_s": abs(sp[k] - sp[k - 300]) / max(1e-9, sum(abs(y - x) for x, y in zip(sp[k - 300 : k + 1], sp[k - 299 : k + 1], strict=False))), "drift_s": (sp[k] - sp[k - 300]) / ua * (1 if side == "yes" else -1), "kind": w["kind"], "eff5": eff(A, k, 300), "eff10": eff(A, k, 600), "cross5": xcount(sp, A, k, 300), "reach": abs(A[k] - strike) / ua, "run10": (A[k] - A[k - 600]) / ua * (1 if side == "no" else -1), "u": u, "t_min": t / 60.0, "sp": sp, "ia": ia, "strike": strike})
        break


import numpy as np  # noqa: E402


def fee1(p):
    return 0.07 * p * (1 - p)


def unit_series(mid):
    m = np.asarray(mid, dtype=float)
    d2 = np.concatenate(([0.0], np.cumsum(np.diff(m) ** 2)))
    u = np.zeros(len(m))
    for s_ in range(len(m)):
        lo = max(0, s_ - 900)
        cnt = s_ - lo
        if cnt >= 30:
            u[s_] = math.sqrt((d2[s_] - d2[lo]) / cnt) * math.sqrt(60.0)
    return u


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def plose_series(b):
    """p_lose at every market second, as the board reads it: None where there is no spot reading."""
    sp, ia, n, strike, side = b["sp"], b["ia"], b["n"], b["strike"], b["side"]
    arr = np.array([x if x is not None else np.nan for x in sp], dtype=float)
    idx = np.where(~np.isnan(arr), np.arange(len(arr)), 0)
    np.maximum.accumulate(idx, out=idx)
    arr = arr[idx]  # a gap carries the last value forward
    cs = np.concatenate(([0.0], np.cumsum(arr)))
    dd = np.diff(arr) ** 2
    dcs = np.concatenate(([0.0], np.cumsum(dd)))
    out = [None] * n
    cz = [None] * n
    for s_ in range(n):
        k = ia + s_
        if k - 600 < 0 or k + 1 > len(arr):
            continue
        avg = (cs[k + 1] - cs[k - 59]) / 60.0
        unit = math.sqrt((dcs[k] - dcs[k - 600]) / 600.0) * math.sqrt(60.0)
        if unit <= 0:
            continue
        cushion = (avg - strike) if side == "yes" else (strike - avg)
        out[s_] = norm_cdf(-cushion / (unit * math.sqrt(max(n - s_, 1.0) / 60.0)))
        cz[s_] = cushion / unit
    return out, cz


def run(b, cushion, k=1.0, p=1.0, arm=1.0):
    """(live staircase, give-back, combined) as (net dollars, second sold or None), and the p_lose read at the give-back's sale."""
    ask, bid, t, n, U, PL = b["ask"], b["bid"], b["t"], b["n"], b["U"], b["PL"]
    basis = ask + fee1(ask)
    cost = QTY * ask + fee(ask)
    hold = b["H"]
    best, hi_bid = -9.0, 0.0
    d, hi, lo, last = 0, 0.0, 0.0, 0.0
    out_s = out_g = None
    s_hi, lo_bid, b0 = t, 9.0, bid[t]
    if not cushion:
        b["fe"] = None  # the features belong to the cushion-off run; the cushion-on run leaves them alone
    for s_ in range(t + 1, n - 3):
        x = bid[s_]
        net = x - fee1(x) - basis
        best = max(best, net)
        if x > hi_bid:
            s_hi = s_
        hi_bid = max(hi_bid, x)
        lo_bid = min(lo_bid, x)
        u = U[s_]
        if u <= 0:
            continue
        cf = (2.0 * (1.0 - PL[s_])) if (cushion and PL[s_] is not None) else 1.0
        rev = max(0.01, k * u * (max(n - s_, 60.0) / 60.0) ** p * cf)
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
            if not cushion and b["CZ"][s_] is not None and b["CZ"][t] is not None:
                run_up = hi_bid - b0
                b["fe"] = {"time left (s)": n - s_, "bid at the sale": x, "share of the run given back": (hi_bid - x) / run_up if run_up > 0 else 0.0, "seconds since the high": s_ - s_hi, "bid range since the buy (bid typical minutes)": (hi_bid - lo_bid) / u, "spot cushion (spot typical minutes)": b["CZ"][s_], "spot cushion change since the buy": b["CZ"][s_] - b["CZ"][t]}
        if out_s is not None and out_g is not None:
            break
    pay = lambda s_: QTY * bid[s_] - fee(bid[s_]) - cost
    st = pay(out_s) if out_s is not None else hold
    gb = pay(out_g) if out_g is not None else hold
    firsts = [x for x in (out_s, out_g) if x is not None]
    cb = pay(min(firsts)) if firsts else hold
    return (st, out_s), (gb, out_g), (cb, min(firsts) if firsts else None), (PL[out_g] if out_g is not None else None)


print(f"{len(bets)} bets")
for b in bets:
    b["U"] = unit_series(b["mid"])
    b["PL"], b["CZ"] = plose_series(b)
    for tag, flag in (("off", False), ("on", True)):
        (b["S" + tag], b["Ss" + tag]), (b["G" + tag], b["Gs" + tag]), (b["C" + tag], b["Cs" + tag]), b["pl" + tag] = run(b, flag)
have = sum(1 for b in bets if b["PL"][b["t"]] is not None)
print(f"bets with a spot reading at the buy: {have} of {len(bets)}")


def line(rows, label, tag):
    n = len(rows)
    if not n:
        return
    f = lambda key: sum(r[key + tag] for r in rows)
    sold = lambda key: sum(1 for r in rows if r[key + "s" + tag] is not None)
    print(f"  cushion {tag:3s} {label:26s} n={n:5d} hold {sum(r['H'] for r in rows):7.0f} | staircase {f('S'):7.0f} (sold {sold('S')}) | give-back {f('G'):7.0f} (sold {sold('G')}) | combined {f('C'):7.0f} (sold {sold('C')})")


for lab, rows in (("all bets", bets), ("crypto", [b for b in bets if b["kind"] == "crypto"]), ("commodity", [b for b in bets if b["kind"] == "commodity"])):
    line(rows, lab, "off")
    line(rows, lab, "on")
print()
sold_rows = [b for b in bets if b["Gsoff"] is not None and b["ploff"] is not None]
print(f"the cushion-off give-back sold {sum(1 for b in bets if b['Gsoff'] is not None)} bets; {len(sold_rows)} with a p_lose reading at the sale")
xs = sorted(b["ploff"] for b in sold_rows)
c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
print(f"p_lose at the give-back's sale, thirds: cuts {c1:.3f} / {c2:.3f} (low = the spot backs the bet)")
for lab, test in (("low  (backed)", lambda v: v <= c1), ("mid", lambda v: c1 < v <= c2), ("high (not backed)", lambda v: v > c2)):
    rr = [b for b in sold_rows if test(b["ploff"])]
    n = len(rr)
    won = sum(1 for b in rr if b["won"])
    gain = sum(b["Goff"] - b["H"] for b in rr)
    print(f"  {lab:18s} n={n:4d} won {won / n:.2f} | give-back minus hold {gain:8.0f} | saved on losers {sum(b['Goff'] - b['H'] for b in rr if not b['won']):7.0f} | cost on winners {sum(b['Goff'] - b['H'] for b in rr if b['won']):7.0f}")
print()
rows = [b for b in bets if b["Gsoff"] is not None and b["fe"] is not None]
print(f"features read at the give-back's sale (cushion off), {len(rows)} bets; thirds are this sample's cuts; give-back minus hold in dollars")
for name in rows[0]["fe"]:
    xs = sorted(b["fe"][name] for b in rows)
    c1, c2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    print(f"{name}: cuts {c1:.3f} / {c2:.3f}")
    for lab, test in (("low ", lambda v: v <= c1), ("mid ", lambda v: c1 < v <= c2), ("high", lambda v: v > c2)):
        rr = [b for b in rows if test(b["fe"][name])]
        n = len(rr)
        print(f"   {lab} n={n:4d} won {sum(1 for b in rr if b['won']) / n:.2f} | give-back minus hold {sum(b['Goff'] - b['H'] for b in rr):7.0f} | saved {sum(b['Goff'] - b['H'] for b in rr if not b['won']):6.0f} cost {sum(b['Goff'] - b['H'] for b in rr if b['won']):6.0f}")
print(f"\n{time.time() - t_start:.0f} s")
