"""Q4: how the leader emerges inside a Crypto Leader window, on Kalshi's own quotes only.
Leader at second t = coin with the highest mid. For t = 1, 3, 5, 8, 10, 12 minutes into the window: how often is that leader the final winner, against the leader's own mid (is it calibrated?) and its ask; how many times the leader's identity changes after t; the second-ranked coin's win rate; by leader-mid band. Also lead changes per window. Windows from crypto_lead_common (n and days printed). Bands fixed before looking.
Run: PYTHONPATH=scripts .venv/bin/python scripts/crypto_lead_q4_emergence.py > studies/results/2026-11-19-crypto-lead-q4-emergence.txt"""
import numpy as np

from crypto_lead_common import load_windows

W = load_windows()
print(f"windows n={len(W)}, {W[0]['day']} to {W[-1]['day']}")
M = []
for w in W:
    m = (w["bid"] + w["ask"]) / 2
    M.append(m)
print("\nleader identity changes per window (highest mid, ignoring seconds with a missing quote):")
ch = []
for m in M:
    mm = np.where(np.isnan(m), -1, m)
    ld = mm.argmax(0)
    ch.append(int((np.diff(ld[60:]) != 0).sum()))
print("  changes: mean %.1f median %.0f quartiles %s" % (np.mean(ch), np.median(ch), np.quantile(ch, [.25, .75])))
print("\nleader at minute t vs final winner")
print("t_min  n   leader_wins  mean_mid  mean_ask  gap_vs_mid  stays_leader_to_end(share of windows leader never changes after t)")
for tmin in (1, 3, 5, 8, 10, 12):
    k = tmin * 60
    n = lw = 0; mids = []; asks = []; stay = 0; sec_w = 0
    for w, m in zip(W, M):
        if np.isnan(m[:, k]).any():
            continue
        mm = np.where(np.isnan(m), -1, m)
        o = np.argsort(-m[:, k]); l = o[0]
        n += 1; lw += bool(w["win"][l]); mids.append(m[l, k]); asks.append(w["ask"][l, k]); sec_w += bool(w["win"][o[1]])
        stay += bool((mm[:, k:].argmax(0) == l).all())
    print(f"{tmin:4d} {n:4d}  {lw/n:.3f}       {np.mean(mids):.3f}    {np.mean(asks):.3f}    {lw/n-np.mean(mids):+.3f}   {stay/n:.3f}   (2nd-ranked wins {sec_w/n:.3f})")
print("\nleader at minute t: win rate by leader-mid band (win rate vs mid and vs ask, n bets are windows x marks of 3,5,8,10,12 min, overlapping)")
bands = [(0, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.9), (0.9, 1.01)]
rows = []
for w, m in zip(W, M):
    for tmin in (3, 5, 8, 10, 12):
        k = tmin * 60
        if np.isnan(m[:, k]).any():
            continue
        l = int(np.argmax(m[:, k]))
        rows.append((m[l, k], w["ask"][l, k], bool(w["win"][l])))
r = np.array(rows, float)
for lo, hi in bands:
    s = r[(r[:, 0] >= lo) & (r[:, 0] < hi)]
    if len(s):
        print(f"  mid {lo:.2f}-{min(hi,1):.2f}: n={len(s):4d} mean_mid={s[:,0].mean():.3f} mean_ask={s[:,1].mean():.3f} winrate={s[:,2].mean():.3f}  gap_vs_mid={s[:,2].mean()-s[:,0].mean():+.3f}  gap_vs_ask={s[:,2].mean()-s[:,1].mean():+.3f}")
print("\nlast-lead-change second (leader final from then on), quantiles over windows:")
ls = []
for m in M:
    mm = np.where(np.isnan(m), -1, m); ld = mm.argmax(0)
    d = np.where(np.diff(ld) != 0)[0]
    ls.append(d[-1] + 1 if len(d) else 0)
print("  ", {q: int(np.quantile(ls, q)) for q in (.1, .25, .5, .75, .9)}, " share where the final leader's identity was set before 5 min:", round(float(np.mean(np.array(ls) < 300)), 3), " final mid leader == winner:", round(float(np.mean([bool(w['win'][np.where(np.isnan(m[:,-1]),-1,m[:,-1]).argmax()]) for w,m in zip(W,M)])), 3))
