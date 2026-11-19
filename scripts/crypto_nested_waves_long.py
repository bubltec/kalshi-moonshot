"""Waves within waves on 24 days: do the larger waves give the odds for the next window? (2026-11-19, user: "the larger waves show our odds on where bets will move ... we can pre-calculate every hour")

One question on the host's settled windows (studies/results/2026-11-19-crypto-close-shares.csv: for each 15-minute close from 2026-10-26 to 2026-11-19 with 4 or more crypto coins settled, the number of coins and how many settled YES; 5 coins up to 11-08, 9 after). D = the share of coins that settled YES less 0.5 (the market-wide direction of the window). Runs of consecutive closes only (a gap breaks the run).
1. Autocorrelation of D at lags 1 to 192 (15 minutes to 2 days) with a permutation band (2.5th to 97.5th of 300 shuffles per lag), lags outside the band and how many to expect by chance; the scales the user named: hour (4), quarter-day (24), day (96).
2. Slow state: the mean of D over the previous 4 (hour), 24 (quarter-day), 96 (day) windows, known before the next window opens. The correlation of each with the NEXT window's D, and the next window's share of 'up' by slow state up/down. That is the 'odds' use: does the larger wave tilt where the next window settles? Shuffle band for each correlation.
3. Time of day (the daily wave): the mean D by ET hour and by six-hour block, with the shuffle band of the largest block and hour deviation.
4. The lag-1 alternation by slow state (hour, quarter-day): does the 15-minute turn depend on the larger wave?
Counts are windows (not independent); 24 days hold 24 daily cycles. The state is on the settled share only (coarse), not the quote curve. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_nested_waves_long.py > studies/results/<date>-crypto-nested-waves-long.txt"""
import csv
import datetime

import numpy as np

rows = list(csv.DictReader(open("studies/results/2026-11-19-crypto-close-shares.csv")))
closes = np.array([int(r["close_ts"]) for r in rows])
D = np.array([int(r["yes"]) / int(r["coins"]) - 0.5 for r in rows])
brk = np.flatnonzero(np.diff(closes) > 900.5)
segs = np.split(np.arange(len(D)), brk + 1)
f = lambda t: datetime.datetime.fromtimestamp(t - 5 * 3600, datetime.timezone.utc)  # noqa: E731
print(f"window: {f(closes[0]):%Y-%m-%d} to {f(closes[-1]):%Y-%m-%d} ET; {len(D)} closes; runs of consecutive closes {len(segs)}, longest {max(len(s) for s in segs)}; mean D {D.mean():+.3f}, share of windows with D>0 {100.0 * np.mean(D > 0):.1f}%")
rng = np.random.default_rng(3)


def acf(Dv, lags):
    out = []
    for lag in lags:
        xs, ys = [], []
        for sg in segs:
            if len(sg) > lag:
                xs.append(sg[:-lag]); ys.append(sg[lag:])
        a_, b_ = np.concatenate(xs), np.concatenate(ys)
        out.append(np.corrcoef(Dv[a_], Dv[b_])[0, 1] if len(a_) > 30 else np.nan)
    return np.array(out)


LAGS = list(range(1, 193))
obs = acf(D, LAGS)
perm = np.array([acf(D[rng.permutation(len(D))], LAGS) for _ in range(300)])
lo, hi = np.nanpercentile(perm, 2.5, axis=0), np.nanpercentile(perm, 97.5, axis=0)
ok = ~np.isnan(obs)
out_ = [(l, o) for l, o, a, b in zip(LAGS, obs, lo, hi) if not np.isnan(o) and (o < a or o > b)]
print(f"--- 1. autocorrelation of D, lags 1-192: {len(out_)} of {int(ok.sum())} lags outside the band ({0.05 * ok.sum():.0f} expected by chance)")
print("  outside: " + ", ".join(f"{l} ({l * 15 / 60:.1f} h) {o:+.2f}" for l, o in out_))
for nm, lag in (("hour", 4), ("quarter-day", 24), ("day", 96)):
    print(f"  {nm:12s} lag {lag:3d}: {obs[lag - 1]:+.3f} (band {lo[lag - 1]:+.3f} to {hi[lag - 1]:+.3f})")


def slow_pairs(win):
    nxt, last, slow, idx = [], [], [], []
    for sg in segs:
        for j in range(win, len(sg) - 1):
            s = sg[j]
            nxt.append(D[s + 1]); last.append(D[s]); slow.append(float(np.mean(D[sg[j - win:j]]))); idx.append(s)
    return np.array(nxt), np.array(last), np.array(slow)


print("--- 2. the larger wave as the odds: the slow state (mean D of the previous N windows) against the NEXT window")
for nm, win in (("hour (4)", 4), ("quarter-day (24)", 24), ("day (96)", 96)):
    nxt, last, slow = slow_pairs(win)
    if len(nxt) < 100:
        print(f"  {nm}: only {len(nxt)} windows")
        continue
    c = np.corrcoef(slow, nxt)[0, 1]
    sims = [np.corrcoef(slow, nxt[rng.permutation(len(nxt))])[0, 1] for _ in range(300)]
    up, dn = slow >= 0, slow < 0
    print(f"  {nm:16s} n {len(nxt):4d} | corr(slow, next D) {c:+.3f} (band {np.percentile(sims, 2.5):+.3f} to {np.percentile(sims, 97.5):+.3f}) | next window up: after a slow-up {100.0 * np.mean(nxt[up] > 0):.1f}% ({int(up.sum())}), after a slow-down {100.0 * np.mean(nxt[dn] > 0):.1f}% ({int(dn.sum())})")
print("--- 3. time of day (ET): mean D by hour and by six-hour block, with the shuffle band of the largest deviations")
hours = np.array([f(c).hour for c in closes])
blocks = hours // 6
def dev(Dv, key, n):
    return np.array([Dv[key == k].mean() for k in range(n)])
ph = np.array([np.abs(dev(D[rng.permutation(len(D))], hours, 24)).max() for _ in range(300)])
pb = np.array([np.abs(dev(D[rng.permutation(len(D))], blocks, 4)).max() for _ in range(300)])
mh, mb = dev(D, hours, 24), dev(D, blocks, 4)
print("  by hour: " + " ".join(f"{h:02d}:{m:+.2f}" for h, m in enumerate(mh)))
print(f"  largest hour deviation {np.abs(mh).max():+.3f} (shuffle 95th percentile {np.percentile(ph, 95):.3f})")
print("  by six-hour block (00-06, 06-12, 12-18, 18-24): " + " ".join(f"{m:+.3f}" for m in mb) + f" | largest {np.abs(mb).max():.3f} (shuffle 95th {np.percentile(pb, 95):.3f})")
print("--- 4. the 15-minute alternation (effect of the last window on the next) by the slow state")
for nm, win in (("hour (4)", 4), ("quarter-day (24)", 24)):
    nxt, last, slow = slow_pairs(win)
    eff = {}
    for sn, sm in (("slow up", slow >= 0), ("slow down", slow < 0)):
        eff[sn] = np.mean(nxt[sm & (last >= 0)]) - np.mean(nxt[sm & (last < 0)])
    diff = eff["slow up"] - eff["slow down"]
    sims = []
    for _ in range(300):
        n2 = nxt[rng.permutation(len(nxt))]
        sims.append((np.mean(n2[(slow >= 0) & (last >= 0)]) - np.mean(n2[(slow >= 0) & (last < 0)])) - (np.mean(n2[(slow < 0) & (last >= 0)]) - np.mean(n2[(slow < 0) & (last < 0)])))
    print(f"  {nm:16s} effect of the last window in slow-up {eff['slow up']:+.3f}, slow-down {eff['slow down']:+.3f}; difference {diff:+.3f} (band {np.percentile(sims, 2.5):+.3f} to {np.percentile(sims, 97.5):+.3f}); n {len(nxt)}")
