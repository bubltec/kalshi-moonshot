"""What volume ratio marks a real burst, coin by coin? Held favorites (bid 0.80-0.97) in the last 7 minutes, from the flow dump. Read-only. Places nothing.

The ratio is the last 3 seconds' volume over 3 times the market's own normal (median) second of the 2 minutes before, and only seconds with most (60%+) of it against the holder count. For each ratio cut and each coin: how many window-sides fire,
how often the favorite's bid then fell 10c or more within 30 s, and what buying the opposite side at that second returns held to the result (fee paid). The window-side's first second at or over the cut is used.
    python scripts/burst_ratio_by_coin.py /data/flow_feats.jsonl
"""
import json
import math
import sys
from collections import defaultdict

import numpy as np

CUTS = (3, 5, 10, 20, 50)


def fee(p):
    return 0.07 * p * (1 - p)


rows = [json.loads(line) for line in open(sys.argv[1])]
by = defaultdict(list)
for r in rows:
    by[(r["series"], r["tk"], r["lost"])].append(r)
for v in by.values():
    v.sort(key=lambda r: -r["left"])
print("coin  cut | window-sides firing (of held) | a 10c drop follows | opposite side wins | return of buying it with fee (+- se)  | a random held second, same coin")
for series in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"):
    groups = [v for (s, _, _), v in by.items() if s == series]
    if not groups:
        continue
    rnd = []
    for v in groups:
        r = v[len(v) // 2]
        p = 1 - r["bid"]
        rnd.append((r["lost"] - p - fee(p)) / (p + fee(p)))
    for cut in CUTS:
        picks = [next((r for r in v if r["burst"] >= cut and r["a3"] > 1.5 * r["w3"] and r["a3"] >= 100), None) for v in groups]
        picks = [p for p in picks if p]
        if len(picks) < 25:
            print(f"{series[2:5]:4} {cut:3}x | only {len(picks)} fire")
            continue
        ret = [((p["lost"] - (1 - p["bid"]) - fee(1 - p["bid"])) / ((1 - p["bid"]) + fee(1 - p["bid"]))) for p in picks]
        print(f"{series[2:5]:4} {cut:3}x | {len(picks):4} of {len(groups)} | {100 * np.mean([p['drop'] for p in picks]):4.0f}% | {100 * np.mean([p['lost'] for p in picks]):4.1f}% | {100 * np.mean(ret):+7.1f}% +-{100 * np.std(ret) / math.sqrt(len(ret)):.0f} | {100 * np.mean(rnd):+6.1f}% (n={len(rnd)})")
