"""Which order-flow behaviors warn of a collapse? Fit on earlier windows, scored on later ones. Read-only analysis of the dump from scripts/flow_features_dump.py. Places nothing.

Target: the held favorite's bid falls 10c or more within the next 30 seconds (`drop`). Features: contracts traded against the side in the last 1, 3 and 10 seconds, with it in the last 3, trades in the last 3 seconds, the burst (the last 3 seconds' volume over the market's own median second),
the bid's move over the last 5 and 30 seconds, the spread, the bid level and the seconds left. Logistic regression on log-scaled flows; reported: the base rate, the rate among the highest-scored seconds, and what selling at the first high-scored second would have paid against holding.
    python scripts/flow_pattern_study.py /data/flow_feats.jsonl        (needs numpy)
"""
import json
import sys
from collections import defaultdict

import numpy as np

rows = [json.loads(line) for line in open(sys.argv[1])]
rows.sort(key=lambda r: r["t"])
n = len(rows)
NAMES = ["log against 1s", "log against 3s", "log against 10s", "log with 3s", "log trades 3s", "log burst", "bid move 5s", "bid move 30s", "spread", "bid", "seconds left", "against share 3s"]


def feats(r):
    a3, w3 = r["a3"], r["w3"]
    return [np.log1p(r["a1"]), np.log1p(a3), np.log1p(r["a10"]), np.log1p(w3), np.log1p(r["n3"]), np.log1p(r["burst"]), r["dbid5"] * 10, r["dbid30"] * 10, r["spread"] * 10, r["bid"], r["left"] / 100.0, a3 / (a3 + w3 + 1.0)]


X = np.array([feats(r) for r in rows], dtype=float)
y = np.array([r["drop"] for r in rows], dtype=float)
t = np.array([r["t"] for r in rows])
cut = np.quantile(t, 0.6)
tr, te = t <= cut, t > cut
STRUCT = [8, 9, 10]  # spread, bid, seconds left
FLOW = [0, 1, 2, 3, 4, 5, 11]
MOVE = [6, 7]


def fit_score(cols):
    Xc = X[:, cols]
    mu, sd = Xc[tr].mean(0), Xc[tr].std(0) + 1e-9
    Z1 = np.hstack([np.ones((n, 1)), (Xc - mu) / sd])
    b = np.zeros(Z1.shape[1])
    for _ in range(30):
        p = 1 / (1 + np.exp(-Z1[tr] @ b))
        w = p * (1 - p) + 1e-9
        g = Z1[tr].T @ (y[tr] - p) - 1.0 * np.r_[0, b[1:]]
        h = (Z1[tr] * w[:, None]).T @ Z1[tr] + 1.0 * np.diag(np.r_[0, np.ones(len(b) - 1)])
        b = b + np.linalg.solve(h, g)
    return Z1 @ b, b


def auc(score):
    pos, neg = score[te][y[te] == 1], score[te][y[te] == 0]
    neg = neg[:: max(1, len(neg) // 4000)]
    return float((pos[:, None] > neg[None, :]).mean())


def top_rate(score, pct):
    k = int(te.sum() * pct / 100)
    return float(y[te][np.argsort(-score[te])][:k].mean())


print(f"{n:,} held-favorite seconds ({int(tr.sum()):,} to fit, {int(te.sum()):,} to score); a 10c drop within 30 s follows {100 * y[te].mean():.1f}% of the scored seconds")
models = {"structure only (bid, seconds left, spread)": STRUCT, "flow only": FLOW, "bid moves only (5 s, 30 s)": MOVE, "structure + flow": STRUCT + FLOW, "structure + flow + bid moves": STRUCT + FLOW + MOVE}
scores = {}
for name, cols in models.items():
    sc, b = fit_score(cols)
    scores[name] = sc
    print(f"  {name:44} AUC {auc(sc):.3f}; a drop follows {100 * top_rate(sc, 1):4.1f}% of its top 1% and {100 * top_rate(sc, 5):4.1f}% of its top 5%")
sc, b = fit_score(STRUCT + FLOW + MOVE)
names = [NAMES[c] for c in STRUCT + FLOW + MOVE]
print("  full model weights per standard deviation: " + ", ".join(f"{nm} {w:+.2f}" for nm, w in sorted(zip(names, b[1:], strict=True), key=lambda p: -abs(p[1]))))

print("\nselling at the first second a model puts in its top few percent (per window-side held), holding minus selling per contract after the sale fee (negative = selling was better):")
for name in ("structure only (bid, seconds left, spread)", "structure + flow + bid moves"):
    for pct in (1, 3):
        thr = np.quantile(scores[name][te], 1 - pct / 100)
        seen = {}
        for i in np.where(te)[0]:
            k = (rows[i]["tk"], rows[i]["lost"])
            if scores[name][i] >= thr and k not in seen:
                seen[k] = rows[i]["hold_minus_sell"]
        d = list(seen.values())
        if len(d) > 20:
            print(f"  {name:44} top {pct}%: {len(d):4} window-sides fire; {100 * np.mean(d):+6.2f}c +-{100 * np.std(d) / np.sqrt(len(d)):.2f}")
