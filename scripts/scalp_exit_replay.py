"""Replay of the cheap-side swing scalp and three exits on the same bets (2026-11-21, user: scalps like the last three BTC ones; "we don't hold if it doesn't flip, approach a flip, or trend towards a win by the 2 min mark ... 2 mins is not absolute, if it is close we should give a little more time"; the time-to-go step is NOT used, user: it sold things for dipping out of band).
Question: on the same entries, what do these exits make net of the fee on every leg, against holding to settlement?
  H  hold to settlement.
  T  take profit only: sell at the bid when the bid is up TP typical minutes on the entry, or when the side has flipped (bid at or above 0.50); otherwise hold to settlement.
  F  T plus a check at 120 s left: sell at the bid unless the side has flipped, is within NEAR typical minutes of a flip, or is up TREND typical minutes on the entry.
  X  T plus the same test with a deadline that moves with closeness: a bet failing the test at 120 s left gets until 120 - 80*(1 - d/d0) s left (d = distance of the bid to 0.50 now, d0 = at entry, both in typical minutes; never below 40 s), re-tested every second, sold on the first failing second at or after its deadline.
Entry (one per window, either side, Kalshi quotes only): 120 to 660 s after the open, the side's ask 0.18 to 0.40, and down at least DROP typical minutes from its high of the last 180 s. 25 contracts, bought at the ask, taker fee ceil(0.07*C*P*(1-P)) on the buy and on any sale before settlement (the user's own sales were makers, so this is the conservative side). A typical minute = the series' root-mean-square one-second move of the yes mid times sqrt(60) over its previous 4 windows. Settings TP=3, NEAR=1, TREND=1, DROP=2 were set before seeing results.
Anomaly: a tick whose bid is more than 3 typical minutes from its 30-second median is no reading; the checks and the take profit skip it and it is counted.
Curve shape at the buy (in place of the curve-model features, which this replay does not load): slope = the side's ask now against 60 s ago, falling or not; bend = the last 30 s change against the 30 s before it, turning up or not. Series names are labels in one table only.
    scripts/cloud_job.sh scripts/scalp_exit_replay.py DAYS=3
Read-only."""

import collections
import math
import os
import statistics
import time

from kalshi_moonshot import config, db

os.nice(10)
DAYS = float(os.environ.get("DAYS", "3"))
TP, NEAR, TREND, DROP = 3.0, 1.0, 1.0, 2.0
QTY = 25
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
end = float(tape.execute("SELECT max(ts) AS m FROM quotes").fetchone()["m"])
t0 = end - DAYS * 86400
print(f"period {time.strftime('%m-%d %H:%M', time.gmtime(t0))} to {time.strftime('%m-%d %H:%M', time.gmtime(end))} UTC; TP={TP} NEAR={NEAR} TREND={TREND} DROP={DROP} qty={QTY}")


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


def grid(rows, a, b):
    ob, oa = [None] * (b - a), [None] * (b - a)
    for ts, bid, ask in rows:
        k = int(ts) - a
        if 0 <= k < b - a and bid is not None and ask is not None:
            ob[k], oa[k] = float(bid), float(ask)
    lb = la = None
    for k in range(b - a):
        if ob[k] is None:
            ob[k], oa[k] = lb, la
        else:
            lb, la = ob[k], oa[k]
    return ob, oa


tickers = [r["ticker"] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts > ? AND ticker LIKE 'KX%15M-%'", (t0 - 5400,)).fetchall()]
wins = []
for tk in tickers:
    rows = [(r["ts"], r["bid"], r["ask"]) for r in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>? ORDER BY ts", (tk, t0 - 5401)).fetchall()]
    if len(rows) < 200:
        continue
    first, last = int(rows[0][0]), int(rows[-1][0])
    close = (last // 900 + 1) * 900 if last % 900 else last
    a = close - 900
    if first > a + 120 or last < close - 30:
        continue
    bids, asks = grid(rows, a, close)
    if None in bids:
        continue
    wins.append((tk.split("-")[0], a, bids, asks))
wins.sort(key=lambda w: (w[0], w[1]))
print(len(wins), "whole windows")

hist = collections.defaultdict(list)  # series -> [(sum sq, n)] of previous windows
trips = []  # dicts
anom_skips = 0
for series, a, bids, asks in wins:
    mid = [(x + y) / 2 for x, y in zip(bids, asks)]
    sq = sum((mid[k + 1] - mid[k]) ** 2 for k in range(899))
    prev = hist[series][-4:]
    hist[series].append((sq, 899))
    in_range = a >= t0
    if len(prev) < 2 or not in_range:
        continue
    unit = math.sqrt(sum(p[0] for p in prev) / sum(p[1] for p in prev)) * math.sqrt(60)
    if unit <= 0:
        continue
    yes_won = mid[-1] > 0.5
    entry = None
    for i in range(120, 661):
        for side in ("y", "n"):
            ask = asks[i] if side == "y" else 1 - bids[i]
            if not 0.18 <= ask <= 0.40:
                continue
            hi = max((asks[j] if side == "y" else 1 - bids[j]) for j in range(max(0, i - 180), i))
            if hi - ask < DROP * unit:
                continue
            ser = [asks[j] if side == "y" else 1 - bids[j] for j in range(i - 60, i + 1)]
            slope_falling = ser[-1] < ser[0]
            bend_up = (ser[-1] - ser[-31]) > (ser[-31] - ser[0])
            entry = (i, side, ask, (slope_falling, bend_up))
            break
        if entry:
            break
    if not entry:
        continue
    i0, side, ask0, shape = entry
    sb = bids if side == "y" else [1 - x for x in asks]  # the side's bid each second
    won = yes_won if side == "y" else (not yes_won)
    settle = (1.0 if won else 0.0) * QTY - ask0 * QTY - fee(ask0)
    d0 = max(0.5 - sb[i0], 1e-9) / unit

    def anomalous(i):
        win = sorted(sb[max(0, i - 30):i + 1])
        return abs(sb[i] - win[len(win) // 2]) > 3 * unit

    def sell(i):
        return sb[i] * QTY - ask0 * QTY - fee(ask0) - fee(sb[i])

    def passes(i):
        d = (0.5 - sb[i]) / unit
        return sb[i] >= 0.5 or d <= NEAR or (sb[i] - ask0) >= TREND * unit

    res = {"H": settle}
    for name in ("T", "F", "X"):
        out = None
        check_failed_at = None
        for i in range(i0 + 1, 900):
            left = 900 - i
            if left <= 1:
                break
            if anomalous(i):
                anom_skips += 1
                continue
            if sb[i] >= 0.5 or (sb[i] - ask0) >= TP * unit:
                out = sell(i)
                break
            if name == "F" and left <= 120 and not passes(i):
                out = sell(i)
                break
            if name == "X" and left <= 120:
                if passes(i):
                    check_failed_at = None
                else:
                    if check_failed_at is None:
                        d = max(0.5 - sb[i], 0) / unit
                        frac = min(d / d0, 1.0)
                        check_failed_at = max(120 - 80 * (1 - frac), 40)
                    if left <= check_failed_at:
                        out = sell(i)
                        break
        res[name] = out if out is not None else settle
    trips.append({"series": series, "shape": shape, "res": res, "price": ask0})

print(f"{len(trips)} bets from {len(wins)} windows; {anom_skips} anomaly ticks skipped")
for name, label in (("H", "hold to settlement"), ("T", "take profit only"), ("F", "fixed 120 s check"), ("X", "moving deadline")):
    r = [t["res"][name] for t in trips]
    h = [t["res"]["H"] for t in trips]
    beat = sum(1 for x, y in zip(r, h) if x > y + 1e-9)
    lost = sum(1 for x, y in zip(r, h) if x < y - 1e-9)
    print(f"{name} {label:22} n={len(r)} net ${sum(r):8.2f} win rate {sum(1 for x in r if x > 0)/max(len(r),1):4.0%} beat H {beat:3d} lost to H {lost:3d}")
print("\nby curve shape at the buy (net $ for H, T, F, X; n)")
for key in sorted({t["shape"] for t in trips}):
    L = [t for t in trips if t["shape"] == key]
    print(f"  falling={key[0]!s:5} bending_up={key[1]!s:5} n={len(L):3d}  " + "  ".join(f"{m} {sum(t['res'][m] for t in L):8.2f}" for m in "HTFX"))
print("\nby series (label only): n, net H and X")
by = collections.defaultdict(list)
for t in trips:
    by[t["series"]].append(t)
for sr, L in sorted(by.items(), key=lambda kv: -len(kv[1])):
    print(f"  {sr:14} n={len(L):3d}  H {sum(t['res']['H'] for t in L):8.2f}  X {sum(t['res']['X'] for t in L):8.2f}")
print("DONE")
