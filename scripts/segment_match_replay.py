"""Segment matching, first test: does what followed the best-matching 3-minute segment of the last hour say where the spot goes over the next 3 minutes?

User, 2026-11-17: "chunk the market into 3 min segments; rate each one going back an hour ... shape, direction, volatility; place our current spot into a segment that matches the current 3 min period template the most. and it should match at least one on a deviation (wave patterns repeat -- but not forever, 2, 3 times maybe at most ...) see what the next segment looks like; now we can make a decision on whether we buy, sell, or hold".
This test is only the step every decision rests on. No contract, no buy, no sale.

How a segment is read: the spot every 30 s; a 3-minute segment is its six 30 s moves, added up from the segment's start and put in the market's own units (the typical 30 s move over the hour before: root-mean-square). The six running sums ARE the shape, the direction (the last one) and the volatility (how far they swing).
The match: the last 3 minutes against every 3-minute window of the hour before that ended at least 3 minutes ago (rolling, every 30 s, so a wave that began mid-block is found). Distance: the root-mean-square gap between the two shapes, in typical 30 s moves. The best match is the nearest. "Repeats": how many windows that do not overlap each other sit within one typical move of the current shape.
The forecast: the net move over the 3 minutes that followed the best match (and the average over the non-overlapping repeats). Scored against the spot's actual next 3 minutes, at one moment a minute per index.
Compared with the two forecasts that need no matching: the last 3 minutes simply continue, or simply reverse.
Window: 2026-11-09 on. Every index on the tape with a tick in each 30 s (classified by curve, not by asset: all are pooled). Settings above fixed before the run.

    .venv/bin/python scripts/segment_match_replay.py
"""
import datetime
import glob
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.parquet as pq

ROOT = "data/tape_archive"
ET = ZoneInfo("America/New_York")
STEP, SEG, HOUR = 30.0, 6, 120  # seconds a step; steps in a 3-minute segment; steps in the hour searched
NEAR = 1.0  # a "repeat": within this many typical 30 s moves of the current shape (root-mean-square)

parts = defaultdict(list)
for f in sorted(glob.glob(f"{ROOT}/index_ticks/day=*/part-0.parquet")):
    t = pq.read_table(f, columns=["ts", "index_id", "value"])
    ids, ts, vs = np.array(t["index_id"].to_pylist()), t["ts"].to_numpy(), t["value"].to_numpy()
    for i in np.unique(ids):
        m = (ids == i) & (vs > 0)
        parts[i].append((ts[m], vs[m]))

rows = []  # (day, distance, repeats, forecast best, forecast repeats, continue, actual, volatility of the current segment)
kept = []
for ix, ps in sorted(parts.items()):
    ts, vs = np.concatenate([p[0] for p in ps]), np.concatenate([p[1] for p in ps])
    o = np.argsort(ts, kind="stable")
    ts, vs = ts[o], vs[o]
    g = np.arange(np.ceil(ts[0] / STEP) * STEP, ts[-1], STEP)
    j = np.searchsorted(ts, g, side="right") - 1
    fresh = (g - ts[j]) <= STEP  # a tick inside the step
    if fresh.mean() < 0.9:
        continue  # an index that does not tick every 30 s cannot be read in 30 s steps
    kept.append(ix)
    lg = np.log(vs[j])
    mv = np.r_[0.0, np.diff(lg)]  # mv[k]: the move over the step ending at k
    okm = np.r_[False, fresh[1:] & fresh[:-1]]
    sq = np.cumsum(np.where(okm, mv * mv, 0.0))
    nn = np.cumsum(okm.astype(float))
    cum = np.cumsum(mv)
    bad = np.cumsum(~okm)
    n = len(g)
    for k in range(HOUR + 2 * SEG, n - SEG, 2):  # one moment a minute
        if bad[k + SEG] - bad[k - HOUR - SEG] > 0:
            continue  # a stale step anywhere in the hour searched, the current segment or the next one
        cnt = nn[k] - nn[k - HOUR]
        u = np.sqrt((sq[k] - sq[k - HOUR]) / cnt) if cnt > 0 else 0.0
        if u <= 0:
            continue
        cur = (cum[k - SEG + 1:k + 1] - cum[k - SEG]) / u  # the six running sums of the last 3 minutes
        ends = np.arange(k - SEG, k - HOUR, -1)  # candidate windows end at least 3 minutes ago, inside the hour
        cand = (cum[ends[:, None] - SEG + 1 + np.arange(SEG)[None, :]] - cum[ends - SEG][:, None]) / u
        dist = np.sqrt(((cand - cur) ** 2).mean(1))
        nxt = (cum[ends + SEG] - cum[ends]) / u  # what each candidate did over its next 3 minutes
        b = int(np.argmin(dist))
        near = np.flatnonzero(dist <= NEAR)
        chosen = []
        for i in near[np.argsort(dist[near])]:  # non-overlapping repeats, nearest first
            if all(abs(ends[i] - ends[c]) >= SEG for c in chosen):
                chosen.append(i)
        actual = (cum[k + SEG] - cum[k]) / u
        rows.append((datetime.datetime.fromtimestamp(g[k], ET).strftime("%m-%d"), dist[b], len(chosen), nxt[b], float(np.mean(nxt[chosen])) if chosen else np.nan, cur[-1], actual,
                     float(np.abs(np.diff(np.r_[0.0, cur])).sum())))

day = np.array([r[0] for r in rows])
D = np.array([r[1:] for r in rows], dtype=float)
dist, reps, f_best, f_rep, cont, actual, vol = D.T
days = sorted(set(day))[1:]
keep = np.isin(day, days)
day, dist, reps, f_best, f_rep, cont, actual, vol = day[keep], dist[keep], reps[keep], f_best[keep], f_rep[keep], cont[keep], actual[keep], vol[keep]
print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(actual)} moments (one a minute) on {len(kept)} indexes that tick every 30 s: {', '.join(kept)}")
print("moves are in the market's own typical 30 s moves; a typical 3 minutes is about 2.4 of them. 'right way' counts only moments where the forecast and the actual move are both at least half a typical 30 s move\n")


def score(label, f, m):
    m = m & np.isfinite(f)
    big = m & (np.abs(f) >= 0.5) & (np.abs(actual) >= 0.5)
    if m.sum() < 50 or big.sum() < 30:
        return f"  {label:42s} too few"
    hit = np.mean(np.sign(f[big]) == np.sign(actual[big]))
    per = [np.mean(np.sign(f[big & (day == d)]) == np.sign(actual[big & (day == d)])) for d in days if (big & (day == d)).sum() >= 30]
    c = np.corrcoef(f[m], actual[m])[0, 1]
    return f"  {label:42s} moments {int(m.sum()):7d} | called the right way {100 * hit:5.1f}% of {int(big.sum()):6d} | correlation with the actual move {c:+.3f} | days over 50%: {sum(p > 0.5 for p in per)} of {len(per)}"


allm = np.ones(len(actual), bool)
print("THE FORECASTS, every moment")
print(score("what followed the best match", f_best, allm))
print(score("the average of what followed the repeats", f_rep, allm))
print(score("no matching: the last 3 minutes continue", cont, allm))
print(score("no matching: the last 3 minutes reverse", -cont, allm))
print("\nBY HOW CLOSE THE BEST MATCH IS (thirds of all moments)")
q1, q2 = np.quantile(dist, [1 / 3, 2 / 3])
for lab, m in ((f"closest third (within {q1:.2f})", dist <= q1), (f"middle ({q1:.2f} to {q2:.2f})", (dist > q1) & (dist <= q2)), (f"loosest third (over {q2:.2f})", dist > q2)):
    print(score(lab, f_best, m))
print(f"\nBY HOW MANY TIMES THE SHAPE HAS REPEATED IN THE HOUR (non-overlapping windows within {NEAR:.0f} typical move)")
for lab, m in (("none within the deviation", reps == 0), ("1", reps == 1), ("2", reps == 2), ("3", reps == 3), ("4 or more", reps >= 4)):
    print(score(lab + " (the repeats' average)" if lab != "none within the deviation" else lab + " (best match anyway)", f_rep if lab != "none within the deviation" else f_best, m))
print("\nBY HOW MUCH THE CURRENT 3 MINUTES MOVED ABOUT (the path travelled, thirds): a quiet segment, an ordinary one, a busy one")
v1, v2 = np.quantile(vol, [1 / 3, 2 / 3])
for lab, m in (("quiet", vol <= v1), ("ordinary", (vol > v1) & (vol <= v2)), ("busy", vol > v2)):
    print(score(lab + ", best match", f_best, m))
print("\nBY DAY, the best match")
for d in days:
    print(score(d, f_best, day == d))
print("\nWHEN THE FORECAST IS LARGE (the best match's next 3 minutes moved at least a typical 3 minutes, and the match is in the closest third)")
print(score("large forecast, close match", f_best, (np.abs(f_best) >= 2.4) & (dist <= q1)))
print(score("large forecast, 2 or 3 repeats", f_rep, (np.abs(f_rep) >= 1.5) & (reps >= 2) & (reps <= 3)))
