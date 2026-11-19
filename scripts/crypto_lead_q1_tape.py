"""Q1: what the tape holds for Kalshi's Crypto Leader 15 minute races, and how the five quotes behave through a window.
Reads the archived quotes (2026-11-09 on) and settled results (public API, studies/results/2026-11-19-crypto-lead-results.csv). Reports windows, days, the sum of the five mids and of the five asks and bids (a fair book sums to 1), spreads, and when the leader becomes clear (leader mid >= 0.5, >= 0.8; leader = highest mid) by seconds into the window. No coin is used in any rule; coin names only label the share of wins.
Run on the Mac: .venv/bin/python scripts/crypto_lead_q1_tape.py > studies/results/2026-11-19-crypto-lead-q1-tape.txt"""
from collections import Counter

import numpy as np

from crypto_lead_common import COINS, load_windows

W = load_windows()
print(f"windows with all 5 quotes, results, one winner: n={len(W)}, {W[0]['day']} to {W[-1]['day']}")
print("windows per day:", dict(Counter(w["day"] for w in W)))
print("winner share (label only):", {c: round(sum(w["win"][i] for w in W) / len(W), 3) for i, c in enumerate(COINS)})
mark = [30, 120, 300, 450, 600, 750, 840, 895]
print("\nthrough the window (elapsed seconds): median over windows")
print("elapsed  sum_mid  sum_ask  sum_bid  leader_mid  leader_spread  2nd_mid  other_med_spread")
for k in mark:
    sm, sa, sb, lm, ls, s2, osp = [], [], [], [], [], [], []
    for w in W:
        b, a = w["bid"][:, k], w["ask"][:, k]
        if np.isnan(a).any():
            continue
        m = (a + b) / 2
        o = np.argsort(-m)
        sm.append(m.sum()); sa.append(a.sum()); sb.append(b.sum())
        lm.append(m[o[0]]); ls.append((a - b)[o[0]]); s2.append(m[o[1]]); osp.append(np.median((a - b)[o[1:]]))
    print(f"{k:6d}  {np.median(sm):.3f}   {np.median(sa):.3f}   {np.median(sb):.3f}    {np.median(lm):.3f}      {np.median(ls):.3f}        {np.median(s2):.3f}    {np.median(osp):.3f}   (n={len(sm)})")
print("\nask-sum minus 1 (buying every coin's yes costs this over the 1.00 payout), quantiles over all window-seconds at marks:")
d = [w["ask"][:, k].sum() - 1 for w in W for k in mark if not np.isnan(w["ask"][:, k]).any()]
print("  ", {q: round(float(np.quantile(d, q)), 3) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}, "n=", len(d))
print("bid-sum minus 1:", {q: round(float(np.quantile([w['bid'][:, k].sum() - 1 for w in W for k in mark if not np.isnan(w['bid'][:, k]).any()], q)), 3) for q in (0.1, 0.25, 0.5, 0.75, 0.9)})
print("\nwhen the leader (highest mid) first reaches a mid level and stays at/above it: first second after which leader mid never drops below level")
for lv in (0.5, 0.6, 0.8, 0.9):
    t = []
    for w in W:
        m = ((w["bid"] + w["ask"]) / 2)
        if np.isnan(m).any():
            m = np.where(np.isnan(m), 0, m)
        lead = m.max(0)
        below = np.where(lead < lv)[0]
        t.append(below[-1] + 1 if len(below) else 0)
    t = np.array(t)
    print(f" level {lv}: median {np.median(t):.0f}s, quartiles {np.quantile(t,0.25):.0f}/{np.quantile(t,0.75):.0f}s, never held to the end in {np.mean(t>=899):.1%} (n={len(t)})")
print("\nshare of windows whose leader mid at minute m is at least 0.5:")
for k in (60, 180, 300, 480, 600, 720, 840):
    v = [((w["bid"][:, k] + w["ask"][:, k]) / 2).max() >= 0.5 for w in W if not np.isnan(w["ask"][:, k]).any()]
    print(f"  {k/60:.0f} min: {np.mean(v):.1%} (n={len(v)})")
