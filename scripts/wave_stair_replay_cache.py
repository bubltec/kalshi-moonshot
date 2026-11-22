"""Replay of the wave-top lane's staircase sale (src/kalshi_moonshot/watcher.py `_sell_wave_take`, 2026-11-22) on the study cache.

Question (user, 2026-11-22: "I think 0.25 needs to be variable on time left", "and cushion"): what swing size `rev = max(1c, k x u x (max(T, 60) / 60)^p x g)` sells the wave-top bets best, against holding them to settlement, fee on every leg?
  u = the bid's typical minute (root-mean-square one-second change of the mid over the last 900 s, times sqrt(60); the watcher reads the mid's);
  g = the cushion factor 2 x (1 - P_lose) from the spot's 60 s average against the strike (1 with `cushion` off), the same P_lose the high-zone rule reads;
  the sale: the first second the bid falls under the last confirmed swing low, once the bet has been ahead after both fees (a small loss is accepted); and from a bid of 0.80 up a bet that is ahead is sold when P_lose >= 1 - bid + fee.
The buys are the lane's detector (wavetop.signal_at on the 60 s average against the strike), the lane's price band (LO..HI, 0.18 to 0.55) and its entry window (minute 2 to minute 10), taker fills at the ask (the resting order and its follow are not replayed), 25 contracts, crypto markets only (a commodity's strike is Kalshi's and its spot a perp reference).
Settings are NOT tuned before this replay: the lane's defaults (k 0.25, p 0.5, cushion on) came from two bets; the grid below is read after the fact, so whatever it prefers is a setting chosen after seeing the results (CLAUDE.md rule 4).
    .venv/bin/python scripts/wave_stair_replay_cache.py [CACHE_DIR]
Read-only."""

import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc  # noqa: E402

from kalshi_moonshot import wavetop as wt  # noqa: E402

LO, HI, QTY = float(os.environ.get("LO", "0.18")), float(os.environ.get("HI", "0.55")), 25
ENTRY_FROM, ENTRY_TO = 120, 600
cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else "job-results/20261121-154853-21423")
print(f"{len(cache.windows)} windows, newest close {cache.meta['newest_close_iso']}; band {LO}-{HI}, entry {ENTRY_FROM}-{ENTRY_TO} s, qty {QTY}")
t_start = time.time()


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


def fee1(p):
    return 0.07 * p * (1 - p)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def hold_net(ask, won):
    cost = QTY * ask + fee(ask)
    return (QTY - cost) if won else -cost


def prefix_sq(path):
    """Cumulative sum of squared one-second changes (None-safe: a gap contributes 0), so the rms over any span is O(1)."""
    out, acc = [0.0], 0.0
    for a, b in zip(path, path[1:], strict=False):
        if a is not None and b is not None:
            acc += (b - a) ** 2
        out.append(acc)
    return out


def unit_at(pref, k, span):
    j = max(0, k - span)
    n = k - j
    return math.sqrt((pref[k] - pref[j]) / n) * math.sqrt(60.0) if n >= 30 else 0.0


bets, skipped = [], 0
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["kind"] != "crypto" or w["result"] not in ("yes", "no") or w["strike"] is None:
        continue
    close = int(w["close"])
    a = close - 900
    ia = 3700
    T, V = cache.ticks(w["index"])
    sp = wt.grid(T, V, a - 3700, close)
    if None in sp[ia - 3660 : ia + ENTRY_TO + 1]:
        skipped += 1
        continue
    qt, qb, qa = cache.quotes(w)
    if len(qt) < 100:
        skipped += 1
        continue
    bids, asks = wt.grid(qt, qb, a, close), wt.grid(qt, qa, a, close)
    qfirst = max(0, int(qt[0]) - a + 1)
    if qfirst > ENTRY_TO or None in bids[qfirst:] or None in asks[qfirst:]:
        skipped += 1
        continue
    bids = [bids[qfirst]] * qfirst + bids[qfirst:]
    asks = [asks[qfirst]] * qfirst + asks[qfirst:]
    A = wt.average(sp)
    ua = wt.typical_minute(A, ia)
    if ua <= 0:
        skipped += 1
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
        # the bet: second-by-second readings from the buy to the close
        n = close - a
        bid_s = [(bids[s] if side == "yes" else 1.0 - asks[s]) for s in range(n)]
        mid_s = [0.5 * (bids[s] + asks[s]) for s in range(n)]
        pq = prefix_sq(mid_s)
        pspot = prefix_sq(sp)
        sgn = 1.0 if side == "yes" else -1.0
        plose, ubid = {}, {}
        for s in range(t + 1, n - 3):
            ubid[s] = unit_at(pq, s, 900)
            sk = ia + s
            avg = A[sk] if sk < len(A) else None
            us = unit_at(pspot, sk, 600)
            if avg is not None and us > 0:
                cushion = sgn * (avg - strike)
                plose[s] = phi(-cushion / (us * math.sqrt(max(n - s, 1.0) / 60.0)))
        bets.append({"ticker": w["ticker"], "close": close, "side": side, "t": t, "ask": ask, "won": side == w["result"], "H": hold_net(ask, side == w["result"]), "bid": bid_s, "ubid": ubid, "plose": plose, "n": n})
        break


def sale(b, k, p, cushion_on, high_zone=True, backup=True, stairs=True):
    """The staircase on one bet: (net dollars, sold?, small-loss sale?)."""
    ask, bid, t, n = b["ask"], b["bid"], b["t"], b["n"]
    basis = ask + fee1(ask)
    d, hi, lo, last_low, best = 0, 0.0, 0.0, 0.0, -9.0
    for s in range(t + 1, n - 3):
        x = bid[s]
        net = x - fee1(x) - basis
        best = max(best, net)
        u = b["ubid"].get(s, 0.0)
        pl = b["plose"].get(s)
        g = 2.0 * (1.0 - pl) if (cushion_on and pl is not None) else 1.0
        rev = max(0.01, k * u * (max(n - s, 60.0) / 60.0) ** p * g) if u > 0 else 0.05
        if hi <= 0:
            hi = lo = x
        if d >= 0:
            hi = max(hi, x)
            if hi - x >= rev - 1e-9:
                d, lo = -1, x
        if d < 0:
            lo = min(lo, x)
            if x - lo >= rev - 1e-9:
                last_low, d, hi = lo, 1, x
        hit = stairs and best > 0 and d < 0 and last_low > 0 and x < last_low - 1e-9 and (backup or net > 0)
        if not hit and high_zone and net > 0 and x >= wt.HIGH - 1e-9 and pl is not None and pl >= 1.0 - x + fee1(x):
            hit = True
        if hit:
            return QTY * x - fee(x) - (QTY * ask + fee(ask)), True, net <= 0
    return b["H"], False, False


def report(label, rows, key="E"):
    nn = len(rows)
    xs = [r[key] for r in rows]
    m = sum(xs) / nn
    sd = math.sqrt(sum((v - m) ** 2 for v in xs) / max(nn - 1, 1))
    beat = sum(1 for r in rows if r[key] > r["H"] + 1e-9)
    lost = sum(1 for r in rows if r[key] < r["H"] - 1e-9)
    print(f"  {label:34s} n={nn:4d} total {sum(xs):9.2f} per bet {m:+6.2f} +- {sd / math.sqrt(nn):.2f}  beat hold {beat:4d} lost to hold {lost:4d}  sold {sum(r['sold'] for r in rows):4d}  small-loss sales {sum(r['small_loss'] for r in rows):3d}  worst {min(xs):+7.2f}")


days = (max(b["close"] for b in bets) - min(b["close"] for b in bets)) / 86400.0 if bets else 1.0
print(f"\n{len(bets)} bets over {days:.1f} days ({len(bets) / days:.1f} a day), {skipped} windows skipped for missing spot or quotes; hold to settlement per bet {sum(b['H'] for b in bets) / len(bets):+.2f}, total {sum(b['H'] for b in bets):.2f}")
combos = [(k, p, c) for c in (False, True) for p in (0.0, 0.5, 1.0) for k in (0.1, 0.25, 0.5, 1.0)]
results = {}
for k, p, c in combos:
    rows = []
    for b in bets:
        e, sold, small = sale(b, k, p, c)
        rows.append({**{kk: b[kk] for kk in ("H", "side", "ask", "t", "won")}, "E": e, "sold": sold, "small_loss": small})
    results[(k, p, c)] = rows
print("\nhold to settlement")
report("hold", [{**{kk: b[kk] for kk in ("H",)}, "E": b["H"], "sold": False, "small_loss": False} for b in bets])
for c in (False, True):
    print(f"\nstaircase, cushion {'ON' if c else 'off'} (rev = max(1c, k x u x (T/60)^p{' x g' if c else ''}), high-zone rule on)")
    for p in (0.0, 0.5, 1.0):
        for k in (0.1, 0.25, 0.5, 1.0):
            report(f"k {k} p {p}", results[(k, p, c)])
best = max(results, key=lambda key: sum(r["E"] for r in results[key]))
print(f"\nbest total on this grid (read after the fact): k {best[0]} p {best[1]} cushion {'on' if best[2] else 'off'}")
for lab, sel in (("peak bets (NO)", "no"), ("dip bets (YES)", "yes")):
    print(f"\n{lab}: hold {sum(b['H'] for b in bets if b['side'] == sel):9.2f}")
    for key in ((0.25, 0.5, True), (0.25, 0.5, False), best):
        rows = [r for r in results[key] if r["side"] == sel]
        report(f"k {key[0]} p {key[1]} cushion {'on' if key[2] else 'off'}", rows)
print("\nby the ask paid, the lane's defaults (k 0.25, p 0.5, cushion on) against hold")
for lo, hi in ((0.0, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.56)):
    rows = [r for r in results[(0.25, 0.5, True)] if lo <= r["ask"] < hi]
    if rows:
        print(f"  {lo:.2f}-{hi:.2f}  n={len(rows):4d} hold {sum(r['H'] for r in rows):8.2f}  staircase {sum(r['E'] for r in rows):8.2f}")
print("\nPARTS (lane defaults k 0.25, p 0.5, cushion on)")
for lab, kw in (("staircase + backup + high zone (the lane)", {}), ("staircase, NO small-loss backup, + high zone", {"backup": False}), ("high zone only (no staircase)", {"stairs": False})):
    rows = []
    for b in bets:
        e, sold, small = sale(b, 0.25, 0.5, True, **kw)
        rows.append({"H": b["H"], "E": e, "sold": sold, "small_loss": small})
    report(lab, rows)
print(f"\n{time.time() - t_start:.0f} s")
