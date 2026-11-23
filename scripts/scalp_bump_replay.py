"""[Second part, 2026-11-23, after the user asked for a more defined wave: a BUMP definition, tested beside the old detector and the no-signal baseline. The numbers Z 3, T 0.5, scales 20 / 60 / 180 s are mine, fixed before the run.
  A peak at scale N: within the last N seconds the raw spot made a high that stands at least Z x the spot's typical move at that scale (typical minute x sqrt(N/60)) above the lowest point of the N seconds before the high (the rise), the spot has come off the high by at least T of that typical move (the turn is confirmed) and by no more than half the rise (it is still near the top), and the whole bump is on one side of the target (its base over the target for a peak). A dip is the mirror. The bet is NO on a peak, YES on a dip. A rise is judged in the spot's own units, so a spike (a rise in 20 s) and a slower wave (60 or 180 s) are the same rule at different scales: the price ratio.]

Does the wave-top buy find SCALPS? (2026-11-23; user: "no dip or peak ... these are all long games, which isn't what this lane is for ... our main goal is scalping".)

On the replayed bets (the rebuilt detector wavetop.signal_at on the study cache, crypto and commodity, 11-09 to 11-22 12:15Z, band 0.18 to 0.55, entry minute 2 to 10, taker fill at the ask, 25 contracts, fee on every leg) and on a baseline of NO signal (the same side bought every 30 s in the window at any ask inside the band, both sides): how often does the bid rise after the buy by at least one and two of the bid's own typical minutes net of both fees, within 3 and 6 minutes, and how long does it take? A scalp is a bet that could be sold for a gain soon after the buy; hold to settlement is shown beside it. The bid's typical minute is the rms one-second change of the mid over the last 900 s times sqrt(60). Read-only; a lead only.
    .venv/bin/python scripts/scalp_hit_replay.py [CACHE_DIR]"""

import math
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
    """The bid path after a buy at `ask` at second t: the best net gain per contract within 180 s and 360 s, the seconds to the first gain of one and of two typical minutes, and the hold net per bet."""
    mid = [0.5 * (bids[s] + asks[s]) for s in range(max(0, t - 900), t + 1)]
    ch = [y - x for x, y in zip(mid, mid[1:], strict=False)]
    u = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(60.0) if len(ch) >= 30 else 0.0
    basis = ask + fee1(ask)
    best, t1, t2 = {180: -9.0, 360: -9.0}, None, None
    for s in range(t + 1, min(n - 5, t + 361)):
        x = (bids[s] if side == "yes" else 1.0 - asks[s])
        net = x - fee1(x) - basis
        for h in (180, 360):
            if s - t <= h:
                best[h] = max(best[h], net)
        if u > 0 and t1 is None and net >= u:
            t1 = s - t
        if u > 0 and t2 is None and net >= 2 * u:
            t2 = s - t
    return {"u": u, "best180": best[180], "best360": best[360], "t1": t1, "t2": t2, "H": hold_net(ask, won), "won": won, "kind": kind, "ask": ask}


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
SCALES = (20, 60, 180)
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
        if t0 + 400 >= n0 - 5:
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

    ur = wt.typical_minute(sp, ia)
    spa = np.array([x if x is not None else np.nan for x in sp], dtype=float)
    if ur > 0:
        for N in SCALES:
            un = ur * math.sqrt(N / 60.0)
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
                if t + 400 >= n - 5:
                    break
                bump[N].append({**scalp(bids, asks, side, ask, t, n, side == w["result"], w["kind"]), "t": t})
                break


def rep(rows, label):
    n = len(rows)
    if not n:
        return
    ok = [r for r in rows if r["u"] > 0]
    m = len(ok)
    def share(key, x, h):
        return sum(1 for r in ok if r[key] >= x * r["u"] if key.startswith("best")) / m
    h1_180 = sum(1 for r in ok if r["best180"] >= r["u"]) / m
    h2_180 = sum(1 for r in ok if r["best180"] >= 2 * r["u"]) / m
    h1_360 = sum(1 for r in ok if r["best360"] >= r["u"]) / m
    h2_360 = sum(1 for r in ok if r["best360"] >= 2 * r["u"]) / m
    ts = sorted(r["t1"] for r in ok if r["t1"] is not None)
    med = ts[len(ts) // 2] if ts else float("nan")
    print(f"{label:34s} n={n:5d} 1u in 3 min {h1_180:.2f} 6 min {h1_360:.2f} | 2u in 3 min {h2_180:.2f} 6 min {h2_360:.2f} | median s to 1u {med:5.0f} | win {sum(r['won'] for r in rows) / n:.2f} hold {sum(r['H'] for r in rows) / n:+.2f}/bet")


print(f"{len(bets)} detector bets, {len(base)} baseline entries (no signal)")
for lab, rows in (("detector, all", bets), ("detector, crypto", [b for b in bets if b["kind"] == "crypto"]), ("detector, commodity", [b for b in bets if b["kind"] == "commodity"]), ("baseline, all", base), ("baseline, crypto", [b for b in base if b["kind"] == "crypto"]), ("baseline, commodity", [b for b in base if b["kind"] == "commodity"])):
    rep(rows, lab)
for N in SCALES:
    rep(bump[N], f"bump {N} s, all")
    rep([b for b in bump[N] if b["kind"] == "crypto"], f"bump {N} s, crypto")
    rep([b for b in bump[N] if b["kind"] == "commodity"], f"bump {N} s, commodity")
print(f"\n{time.time() - t_start:.0f} s")
