"""Buy the OPPOSITE side the moment a burst of one-sided trading shows against a held favorite. Replayed from the flow dump (scripts/flow_features_dump.py). Read-only. Places nothing.

A held favorite is a side whose bid is 0.80-0.97 in the last 7 minutes of a 15-minute crypto market. A burst is the first second at which the contracts traded against it in the last 3 seconds reach a bar (or the volume reaches a multiple of the market's own normal second).
At that second the opposite side's ask is 1 - the favorite's bid. Buying it (fee paid) wins exactly when the favorite loses. Reported per trigger: how many window-sides fire, the share where the opposite side then won, its average price, and the return per dollar
held to the result (the fee-free return too), against the same buy at a random held second of the same windows. Standard errors across window-sides.
    python scripts/burst_opposite_study.py /data/flow_feats.jsonl
"""
import json
import math
import random
import sys

import numpy as np

random.seed(3)


def fee(p):
    return 0.07 * p * (1 - p)


rows = [json.loads(line) for line in open(sys.argv[1])]
by = {}
for r in rows:
    by.setdefault((r["tk"], r["lost"]), []).append(r)  # a window-side: the held side (its lost flag is constant for the window)
for v in by.values():
    v.sort(key=lambda r: -r["left"])


def outcome(r):
    price = 1.0 - r["bid"]  # the opposite side's ask
    win = r["lost"]
    c = price + fee(price)
    return (win - c) / c, (win - price) / price, price, win


def summarize(label, picks):
    if len(picks) < 20:
        return f"  {label:46} only {len(picks)}"
    o = np.array([outcome(r) for r in picks])
    se = 100 * o[:, 0].std() / math.sqrt(len(o))
    return f"  {label:46} {len(o):4} fire; opposite won {100 * o[:, 3].mean():4.1f}% at {100 * o[:, 2].mean():4.1f}c; return with fee {100 * o[:, 0].mean():+7.1f}% +-{se:.0f}, fee-free {100 * o[:, 1].mean():+7.1f}%"


print(f"{len(by)} held window-sides")
print(summarize("a random held second (the baseline)", [random.choice(v) for v in by.values()]))
print("\\nfirst second the contracts against the side in the last 3 s reach the bar")
for bar in (3000, 6000, 12000, 25000):
    print(summarize(f"against >= {bar} contracts in 3 s", [next((r for r in v if r["a3"] >= bar), None) for v in by.values() if any(r["a3"] >= bar for r in v)]))
print("\\nfirst second the volume reaches a multiple of the market's normal second, with most of it against the side")
for mult in (5, 10, 20):
    picks = [next((r for r in v if r["burst"] >= mult and r["a3"] > 2 * r["w3"]), None) for v in by.values()]
    print(summarize(f"volume >= {mult}x normal, 2x more against than with", [p for p in picks if p]))
print("\\nthe same with the opposite side priced 5-25c only (the cheap end you described)")
for bar in (3000, 6000, 12000):
    picks = []
    for v in by.values():
        r = next((r for r in v if r["a3"] >= bar), None)
        if r and 0.05 <= 1 - r["bid"] <= 0.25:
            picks.append(r)
    print(summarize(f"against >= {bar} in 3 s, opposite at 5-25c", picks))
