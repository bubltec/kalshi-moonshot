"""Do wave days cluster? (2026-11-19, user: "we can't use news for the 15 min, but we can use it for a daily adjustment")

One question on the host's settled windows (studies/results/2026-11-19-crypto-close-shares.csv, 2026-10-26 to 2026-11-19, 4+ crypto coins per close): a WAVE window is a close where 80% or more of the coins settled the same way (|share YES - 0.5| >= 0.30). Per ET day with 90 or more closes: the number of wave windows (a daily wave count). Is a day's count related to the day before's (the larger wave as a daily regime that can be pre-calculated), to the previous 3-day mean, and does the count rise on days when more coins moved the same way for several windows in a row? Also the same in rolling 6-hour blocks (24 windows): a block's count against the previous block's. Shuffle bands from 500 shuffles of the days (blocks). A daily regime would be an input like 'yesterday had many wave windows', read from the same Kalshi results the lane already holds, not a news item or a coin. Counts are days (about 24) and blocks (about 90): low power for the daily test. Settings chosen after seeing the results are said to be so.
Run on the Mac (argument 8 = the nine-coin closes only): .venv/bin/python scripts/crypto_wave_days.py 8 > studies/results/<date>-crypto-wave-days.txt"""
import csv
import datetime

import numpy as np

import sys
MIN_COINS = int(sys.argv[1]) if len(sys.argv) > 1 else 4  # 8: only closes with the nine-coin set (from 2026-11-09), where the wave share is comparable; a first run mixed 5-coin and 9-coin closes, whose share of one-way windows differs by construction
rows = [r for r in csv.DictReader(open("studies/results/2026-11-19-crypto-close-shares.csv")) if int(r["coins"]) >= MIN_COINS]
ts = np.array([int(r["close_ts"]) for r in rows])
D = np.array([int(r["yes"]) / int(r["coins"]) - 0.5 for r in rows])
wave = (np.abs(D) >= 0.30).astype(int)
day = np.array([(datetime.datetime.fromtimestamp(t - 5 * 3600, datetime.timezone.utc)).strftime("%Y-%m-%d") for t in ts])
days = sorted(set(day))
cnt = {d: (int(wave[day == d].sum()), int((day == d).sum())) for d in days}
good = [d for d in days if cnt[d][1] >= (90 if MIN_COINS < 8 else 85)]
print(f"window: {days[0]} to {days[-1]}; {len(ts)} closes; wave windows (80%+ one way): {int(wave.sum())} ({100.0 * wave.mean():.1f}% of closes); days with 90+ closes: {len(good)}")
print("  daily wave counts (ET day: waves of closes): " + " ".join(f"{d[5:]}:{cnt[d][0]}/{cnt[d][1]}" for d in good))
x = np.array([cnt[d][0] for d in good], float)
rng = np.random.default_rng(5)


def ac(v, lag=1):
    return float(np.corrcoef(v[:-lag], v[lag:])[0, 1])


def band(v, lag=1):
    s = [ac(rng.permutation(v), lag) for _ in range(500)]
    return np.percentile(s, 2.5), np.percentile(s, 97.5)


print(f"--- daily: mean {x.mean():.1f}, sd {x.std():.1f} waves a day")
for lag in (1, 2, 3):
    lo, hi = band(x, lag)
    print(f"  lag {lag} day: autocorrelation {ac(x, lag):+.3f} (band {lo:+.3f} to {hi:+.3f}) over {len(x) - lag} pairs")
prev3 = np.array([x[i - 3:i].mean() for i in range(3, len(x))])
print(f"  previous 3-day mean against today's count: corr {np.corrcoef(prev3, x[3:])[0, 1]:+.3f} over {len(prev3)} days")
print("  days after a below-median day vs an above-median day: mean count " + f"{x[1:][x[:-1] < np.median(x)].mean():.1f} vs {x[1:][x[:-1] >= np.median(x)].mean():.1f}")
# rolling six-hour blocks of 24 consecutive closes
blocks = []
for i in range(0, len(ts) - 24, 24):
    seg = ts[i:i + 24]
    if np.all(np.diff(seg) <= 900.5 * 2) and seg[-1] - seg[0] <= 24 * 900 + 1800:
        blocks.append(int(wave[i:i + 24].sum()))
b = np.array(blocks, float)
lo, hi = band(b, 1)
print(f"--- six-hour blocks of 24 consecutive closes: {len(b)} blocks, mean {b.mean():.1f} waves, sd {b.std():.1f}; lag-1 autocorrelation {ac(b):+.3f} (band {lo:+.3f} to {hi:+.3f})")
print("  by hour of day, waves per close (ET): " + " ".join(f"{h:02d}:{wave[np.array([datetime.datetime.fromtimestamp(t - 5 * 3600, datetime.timezone.utc).hour for t in ts]) == h].mean():.2f}" for h in range(24)))
