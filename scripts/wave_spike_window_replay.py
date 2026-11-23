"""Copied from wave_bid_shape_study.py (2026-11-23); the tail replays the SPIKE sale over a short window of N seconds (user 2026-11-23: "the price change over a certain number of ticks, maybe 10, and a ratio to look for"): a bet that is ahead after both fees is sold at the bid the first second its rise from its lowest bid of the last N s is z times what the bid usually moves in N seconds (rms one-second change of the mid over the last 900 s x sqrt(N)), else held. Z is swept to choose the setting (said to be so: chosen on these bets); read-only.

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



FEE = 0.07


def spike_sale(b, z, N):
    """Sold the first second the bid has risen z x what it usually moves in N seconds: (bid - its lowest of the last N s) / (rms one-second change of the mid over the last 900 s x sqrt(N))."""
    ask, bid, mid, t, n = b["ask"], b["bid"], b["mid"], b["t"], b["n"]
    basis = ask + fee1(ask)
    ssq = [0.0]
    for i in range(len(mid) - 1):
        ssq.append(ssq[-1] + (mid[i + 1] - mid[i]) ** 2)
    for s_ in range(t + 1, n - 3):
        x = bid[s_]
        if x - fee1(x) - basis <= 0:
            continue
        lo_i = max(0, s_ - 900)
        cnt = s_ - lo_i
        if cnt < 30:
            continue
        sd = math.sqrt((ssq[s_] - ssq[lo_i]) / cnt)
        if sd <= 0:
            continue
        rise = x - min(bid[max(0, s_ - N) : s_ + 1])
        if rise / (sd * math.sqrt(N)) >= z:
            return s_, QTY * x - fee(x) - (QTY * ask + fee(ask))
    return None, b["H"]


H = sum(b["H"] for b in bets)
H = sum(b["H"] for b in bets)
print(f"{len(bets)} bets; hold to settlement {H:.2f}")
print("window N seconds, z = rise over what the bid usually moves in N seconds (rms 1-s change x sqrt(N)); sold bets' hold = what those same bets pay held")
for N in (10, 20, 30, 60):
    for z in (3.0, 4.0, 5.0, 6.0, 8.0):
        res = [spike_sale(b, z, N) for b in bets]
        sold = [(b, r) for b, r in zip(bets, res, strict=False) if r[0] is not None]
        pol = sum(r[1] for r in res)
        sh = sum(b["H"] for b, _ in sold)
        ss = sum(r[1] for _, r in sold)
        print(f"  N {N:2d} z {z:3.1f}: sold {len(sold):4d} ({len(sold) / len(bets):.0%}), win {sum(b['won'] for b, _ in sold) / max(1, len(sold)):.2f} held; sold {ss:8.2f} vs held {sh:8.2f}; policy {pol:8.2f} ({pol - H:+8.2f})")
print(f"\n{time.time() - t_start:.0f} s")
