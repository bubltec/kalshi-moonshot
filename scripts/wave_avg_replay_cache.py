"""Replay of the rebuilt wave-top buy (src/kalshi_moonshot/wavetop.py, 2026-11-22) on the study cache, and how the trend over several hours bears on its bets.

Questions (user, 2026-11-22: "it definitely depends on how much it is trending, and i think 1 hour isn't enough time for a pattern to emerge"; "3 hours is more likely to show a general shift"; "it shouldn't be a hard NO"):
  1. What does the new detector buy, how often, and what do those bets make (held to settlement, and with the lane's wave_take sale), fee on every leg, 25 contracts?
  2. Does the trend over the 1, 3, 6, 12 and 24 hours before the open, in the market's own units, sort those bets?
Trend strength = the spot's change over H hours divided by its typical minute (root-mean-square 60-second change over the hour before the open) times sqrt(60 H): the drift against a random walk's own spread, so it needs no asset or dollar figure. It is signed for the bet: positive = the trend runs the bet's way (a rise for a YES bet, a fall for a NO bet), negative = against it.
The detector is the code the lane runs (wavetop.signal_at on the 60-second average against the market's strike, one zigzag stepped second by second from 3,500 s before the open). A signal is a bet when the side's ask is in the lane's band 0.18-0.40 at that second, from the open to 480 s after it, the first such second of the window. Taker fills at the ask (the lane's resting order and its follow are not replayed).
Crypto markets only: the cache's coin ticks are the settlement indexes the strike is in. A commodity's strike is Kalshi's and its spot is the perp reference, which can differ by a basis, so peaks and dips against the target are not read there (open question for the lane, which lists commodity series).
Settings (LEG_A, MIN_MOVE_A, TURN_A, TAKE) were chosen on two markets before this replay (ZEC 18:30, XRP 02:45 on 11-21/22), not tuned on it. Cache range 11-09 to 11-21: the live bets of 11-21/22 are not in it.
    .venv/bin/python scripts/wave_avg_replay_cache.py [CACHE_DIR]
Read-only."""

import bisect
import collections
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc  # noqa: E402

from kalshi_moonshot import wavetop as wt  # noqa: E402

LO, HI, QTY = 0.18, 0.40, 25
ENTRY_END = 480
HORIZONS = (1, 3, 6, 12, 24)
BUCKETS = [(-99, -2), (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 99)]
cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else "job-results/20261121-154853-21423")
print(f"{len(cache.windows)} windows, newest close {cache.meta['newest_close_iso']}; band {LO}-{HI}, entry 0-{ENTRY_END} s, qty {QTY}; LEG_A {wt.LEG_A} MIN_MOVE_A {wt.MIN_MOVE_A} TURN_A {wt.TURN_A} TAKE {wt.TAKE} RESTING {wt.RESTING}")
t_start = time.time()


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


def hold_net(ask, won):
    cost = QTY * ask + fee(ask)
    return (QTY - cost) if won else -cost


def take_net(ask, won, bids, asks, side, t0, last):
    """The lane's wave_take on the bet: +RESTING sells at the bid; +TAKE sells once the bid made no new high for a quarter of the run (never under 3 s); else held."""
    peak, peak_t = 0.0, t0
    for s_ in range(t0 + 1, last):
        b = bids[s_] if side == "yes" else 1.0 - asks[s_]
        if b > peak + 1e-9:
            peak, peak_t = b, s_
        gain = b - ask
        sell = gain >= wt.RESTING - 1e-9 or (gain >= wt.TAKE - 1e-9 and (s_ - peak_t) >= max(3.0, 0.25 * (peak_t - t0)))
        if sell:
            return QTY * b - fee(b) - (QTY * ask + fee(ask)), True, peak
    return hold_net(ask, won), False, peak


bets, signals_dear, skipped, windows_seen = [], 0, 0, 0
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["kind"] != "crypto" or w["result"] not in ("yes", "no") or w["strike"] is None:
        continue
    windows_seen += 1
    close = int(w["close"])
    a = close - 900
    ia = 3700
    T, V = cache.ticks(w["index"])
    sp = wt.grid(T, V, a - 3700, close)
    if None in sp[ia - 3660 : ia + ENTRY_END + 1]:
        skipped += 1
        continue
    qt, qb, qa = cache.quotes(w)
    if len(qt) < 100:
        skipped += 1
        continue
    bids, asks = wt.grid(qt, qb, a, close), wt.grid(qt, qa, a, close)
    qfirst = max(0, int(qt[0]) - a + 1)
    if qfirst > ENTRY_END or None in bids[qfirst:] or None in asks[qfirst:]:
        skipped += 1
        continue
    bids = [bids[qfirst]] * qfirst + bids[qfirst:]
    asks = [asks[qfirst]] * qfirst + asks[qfirst:]
    A = wt.average(sp)
    ua = wt.typical_minute(A, ia)
    ur = wt.typical_minute(sp, ia)
    if ua <= 0 or ur <= 0:
        skipped += 1
        continue
    strike = float(w["strike"])
    z = wt.Zig(ia - wt.LOOK, A[ia - wt.LOOK], ua, 0.0, wt.LEG_A)
    bet = None
    for k in range(ia - wt.LOOK + 1, ia + ENTRY_END + 1):
        z.step(k, A[k])
        t = k - ia
        if t < qfirst or bet is not None:
            continue
        wv, _ = wt.signal_at(z, A[k], sp[k], strike, ua, sp[ia])
        if wv is None:
            continue
        side = wv.side
        ask = asks[t] if side == "yes" else 1.0 - bids[t]
        if not (LO <= ask <= HI):
            signals_dear += 1
            continue
        won = side == w["result"]
        tk, sold, peak = take_net(ask, won, bids, asks, side, t, close - a)
        bet = {"ticker": w["ticker"], "close": close, "side": side, "t": t, "ask": ask, "won": won, "H": hold_net(ask, won), "E": tk, "sold": sold, "peak": peak, "slope": wv.slope, "prev": wv.prev_max, "move": wv.move, "ua": ua}
        trends = {}
        for h in HORIZONS:
            j = bisect.bisect_right(T, a - h * 3600) - 1
            if j >= 0 and T[j] >= a - h * 3600 - 600:
                trends[h] = (sp[ia] - V[j]) / (ur * math.sqrt(60.0 * h))
        bet["trend"] = trends
    if bet is not None:
        bets.append(bet)


def stats(rows, key):
    n = len(rows)
    if n == 0:
        return "n=0"
    xs = [r[key] for r in rows]
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(n - 1, 1))
    return f"n={n:4d} win {sum(r['won'] for r in rows) / n:.2f} ask {sum(r['ask'] for r in rows) / n:.3f} {key} total {sum(xs):9.2f} per bet {m:+6.2f} +- {sd / math.sqrt(n):.2f}"


days = (max(b["close"] for b in bets) - min(b["close"] for b in bets)) / 86400.0 if bets else 1.0
print(f"\n{windows_seen} crypto windows, {skipped} skipped for missing spot or quotes, {signals_dear} signals on a side outside the band; {len(bets)} bets over {days:.1f} days ({len(bets) / days:.1f} a day)")
for label, rows in (("all bets", bets), ("peak bets (NO)", [b for b in bets if b["side"] == "no"]), ("dip bets (YES)", [b for b in bets if b["side"] == "yes"])):
    print(f"\n{label}")
    print("  hold to settlement ", stats(rows, "H"))
    print("  wave_take + hold   ", stats(rows, "E"))
    print(f"  sold by wave_take: {sum(r['sold'] for r in rows)} of {len(rows)}; best bid reached entry +0.20 or more in {sum((r['peak'] - r['ask']) >= 0.20 for r in rows)}")
print("\nby minute after the open")
for lo, hi in ((0, 60), (60, 120), (120, 240), (240, 481)):
    print(f"  {lo:3d}-{hi:3d} s ", stats([b for b in bets if lo <= b["t"] < hi], "H"))

print("\nTREND, signed for the bet (positive = the trend runs the bet's way; the drift against a random walk's spread). Hold to settlement, then with wave_take.")
for h in HORIZONS:
    print(f"\n{h} h before the open")
    for lab, rows0 in (("all    ", bets), ("peak NO", [b for b in bets if b["side"] == "no"]), ("dip YES", [b for b in bets if b["side"] == "yes"])):
        for lo, hi in BUCKETS:
            rows = [b for b in rows0 if h in b["trend"] and lo < b["trend"][h] * (1 if b["side"] == "yes" else -1) <= hi]
            if rows:
                print(f"  {lab} trend for bet {max(lo, -9):+3d}..{min(hi, 9):+3d}  H {stats(rows, 'H')}   E per bet {sum(r['E'] for r in rows) / len(rows):+6.2f}")
print(f"\n{time.time() - t_start:.0f} s")
