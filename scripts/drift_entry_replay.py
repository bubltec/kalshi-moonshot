"""Does the drift of the market's own curve before the buy decide whether to buy? (2026-11-10, user: "hour-drift entry", after studies/results/2026-11-10-wave-review-10d.txt: bets that went with the previous hour's drift netted +0.031 per $, the others about 0; losing waves had a mean hour drift of -0.57 typical minutes at the buy, winning waves +0.40.)
Question: on the same live bets, would skipping the bets that run against the recent drift of their own index (or taking only the ones that run with it) have beaten taking every bet, after the fee?
Reading: for a bet at time t on an index, drift_w = (log price at t - log price w minutes earlier) x (+1 for a YES bet, -1 for a NO bet) / unit, where unit is the typical minute at t: the rms of the one-minute log moves over the previous 60 minutes. Positive = the bet goes with the drift. From per-minute averages of the index's own ticks.
Cutoff c (rule 2, the market's own recent history): the q-th percentile of the same index's own |drift_w| (same unit rule) over the 24 hours before the buy, sampled every minute. Nothing fixed, no asset named.
Rules (grid fixed before the results; the choice among them is made after seeing them and is said to be so):
  skip_against: skip the bet when drift_w <= -c
  only_with: take the bet only when drift_w >= +c
  w = 15 / 30 / 60 / 120 minutes; q = 50 / 75 / 90
Per variant: bets skipped, the change in actual net (what the live book got on those bets, own sales included, less nothing for the skipped ones), the change if every bet had been held to the result, losers avoided / winners skipped, whole-stake losses avoided, the two halves (median ts), curve shapes (terciles of the index's path efficiency over the 15 minutes before the buy, from the minute series). A variant counts only when positive in both halves and in each shape it claims.
Groups are the lanes' own buy rules: crypto (crypto_multiscale + underdog), favorites (commodity energy/wti/precious/industrial), commodity_multiscale, all. No rule keys on a market name.

    scripts/cloud_job.sh scripts/drift_entry_replay.py
"""

import bisect
import collections
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
NOW = time.time()
SINCE = float(os.environ.get("SINCE", NOW - 10 * 86400))
UNTIL = float(os.environ.get("UNTIL", NOW))
WS, QS = (15, 30, 60, 120), (0.50, 0.75, 0.90)

rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,COALESCE(d.outcome,w.result) AS res,w.close_ts "
    "FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.ts<? ORDER BY d.ts",
    (SINCE, UNTIL),
).fetchall()
bets = {}
for r in rows:
    k = (r["ticker"], r["side"], r["lane"])
    b = bets.get(k)
    if b is None:
        bets[k] = dict(r) | {"stake": float(r["stake_usd"]), "n": float(r["contracts"])}
    else:
        b["stake"] += float(r["stake_usd"])
        b["n"] += float(r["contracts"])
        if r["exit_price"]:
            b["exit_price"] = r["exit_price"]
bets = [b for b in bets.values() if b["res"] in ("yes", "no") and b["close_ts"]]

GROUPS = {
    "crypto": lambda l: l.startswith("crypto_multiscale"),
    "favorites": lambda l: l in ("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"),
    "commodity_multiscale": lambda l: l == "commodity_multiscale",
}

_minutes = {}


def minute_series(ix):
    """(first minute, list of log prices per minute or None) for the index over the whole range plus 24 h + 2 h of history."""
    if ix not in _minutes:
        lo = int(SINCE - 27 * 3600) // 60
        got = tape.execute(
            "SELECT FLOOR(ts/60) AS m, AVG(value) AS v FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 GROUP BY 1 ORDER BY 1",
            (ix, lo * 60, UNTIL + 1),
        ).fetchall()
        m0 = int(got[0][0]) if got else 0
        arr = {int(m) - m0: math.log(float(v)) for m, v in got}
        n = (int(got[-1][0]) - m0 + 1) if got else 0
        ser = [arr.get(i) for i in range(n)]
        last = None
        for i in range(n):
            if ser[i] is None:
                ser[i] = last
            else:
                last = ser[i]
        sq = [0.0]
        for a, b in itertools.pairwise(ser):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
        _minutes[ix] = (m0, ser, sq)
    return _minutes[ix]


def unit_at(sq, k):
    if k < 60:
        return None
    v = (sq[k] - sq[k - 60]) / 60.0
    return math.sqrt(v) if v > 0 else None


def drift_at(ser, sq, k, w):
    u = unit_at(sq, k)
    if not u or k - w < 0 or ser[k] is None or ser[k - w] is None:
        return None
    return (ser[k] - ser[k - w]) / u


def features(b):
    ser_name = b["ticker"].split("-")[0]
    ix = variants.crypto_index(ser_name) or variants.biquote_index(ser_name)
    if not ix:
        return None
    m0, ser, sq = minute_series(ix)
    k = int(float(b["ts"]) // 60) - m0
    if k < 24 * 60 + 130 or k >= len(ser):
        return None
    sign = 1.0 if b["side"] == "yes" else -1.0
    out = {}
    for w in WS:
        d = drift_at(ser, sq, k, w)
        if d is None:
            return None
        hist = sorted(abs(x) for x in (drift_at(ser, sq, j, w) for j in range(k - 1440, k)) if x is not None)
        if len(hist) < 600:
            return None
        out[w] = (d * sign, {q: hist[min(len(hist) - 1, int(q * len(hist)))] for q in QS})
    seg = [x for x in ser[k - 15 : k + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 10 and path > 0 else None
    return out, eff


def pnl(b):
    held = (b["n"] if b["res"] == b["side"] else 0.0) - b["stake"]
    act = b["n"] * float(b["exit_price"]) - b["stake"] if b["exit_price"] else held
    return act, held


data = []
for b in bets:
    f = features(b)
    if f:
        act, held = pnl(b)
        data.append((b, f[0], f[1], act, held))
print(f"settled live bets {len(bets)} | with drift readings {len(data)}")


def report(gname, items):
    if len(items) < 30:
        print(f"\n=== {gname}: {len(items)} bets, too few")
        return
    effs = sorted(e for _, _, e, _, _ in items if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(b["ts"]) for b, *_ in items)[len(items) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    stake = sum(b["stake"] for b, *_ in items)
    print(f"\n=== {gname}: {len(items)} bets, ${stake:.0f} staked | actual {sum(a for *_, a, _ in items):+.2f} ({sum(a for *_, a, _ in items) / stake:+.3f}/$), held {sum(h for *_, h in items):+.2f} | shape cutoffs {lo:.2f} {hi:.2f}")
    out = []
    for mode in ("skip_against", "only_with"):
        for w in WS:
            for q in QS:
                skip = []
                for i, (b, f, e, a, h) in enumerate(items):
                    d, cs = f[w]
                    c = cs[q]
                    if (mode == "skip_against" and d <= -c) or (mode == "only_with" and d < c):
                        skip.append(i)
                if not skip:
                    continue
                da = -sum(items[i][3] for i in skip)
                dh = -sum(items[i][4] for i in skip)
                h1 = -sum(items[i][3] for i in skip if float(items[i][0]["ts"]) <= mid)
                h2 = -sum(items[i][3] for i in skip if float(items[i][0]["ts"]) > mid)
                by = collections.defaultdict(float)
                for i in skip:
                    by[shape(items[i][2])] += -items[i][3]
                lose = sum(1 for i in skip if items[i][3] < 0)
                zeros = sum(1 for i in skip if items[i][3] <= -items[i][0]["stake"] + 1e-9)
                out.append((da, (mode, w, q), dh, len(skip), lose, len(skip) - lose, zeros, h1, h2, dict(by)))
    out.sort(key=lambda o: -o[0])
    print("  variants (mode, w minutes, q) by change in actual net from not taking the skipped bets, best 12:")
    for o in out[:12]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[1]}: skipped {o[3]} ({o[4]} lost, {o[5]} won, {o[6]} whole-stake) | actual {o[0]:+.2f}, held {o[2]:+.2f} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
    both = [o for o in out if o[7] > 0 and o[8] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)}")
    for o in both[:6]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[1]}: skipped {o[3]} | actual {o[0]:+.2f} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")


for gname, test in GROUPS.items():
    report(gname, [x for x in data if test(x[0]["lane"])])
report("all lanes", data)
print("DONE")
