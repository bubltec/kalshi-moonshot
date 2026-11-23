"""Does the HOUR's slope change PriskP's odds? (2026-11-23; user, on the BTC 16:30 bet bought NO into an hour that trended up: "hour view also shows a trend up, so it should have nudged that maybe this wasn't a good bet"; "how is this useful data or even telling us whether a slope on the hour view impacts the buy?")
QUESTION (one): on every buy the PriskP checks model made across the whole tape (scripts/priskp_run.py: the five checks all passing in fifths 4-5, minutes 2-14, never an ask of 0.45-0.55, a take at the ask, held to settlement, fee 0.07 p (1-p) on the buy), do the bets bought AGAINST the hour's drift win less than their price implies, and the bets bought WITH it more?
THE HOUR READING (priskp_run.hour_profile, `drift`): the side's log move over the 60 minutes before the buy in the bet's direction, in the market's own typical minute (u15, the last 15 minutes' rms one-tick log move per minute). Positive = the hour moved the bet's way; negative = against it. Nothing about the hour is in the checks (they read 1, 3 and 30 minutes).
READ: the bets are cut into fifths of that drift by this sample's own cuts (said so: cuts chosen from the data, no setting is chosen). Per group: bets, the share that won, the average ask paid, the gap (won share less ask paid: the edge before the fee), the net per $1 staked after the fee with its standard error. A group counts only with its error margin in view; the gap between the lowest and the highest fifth is shown with its own error. A lead only: read-only.
Run: .venv/bin/python scripts/priskp_hour_slope_study.py PATH/priskp_buys_crypto.json.gz [OUT.png]"""
import gzip
import json
import math
import sys
from pathlib import Path

path = Path(sys.argv[1])
rows = json.loads(gzip.decompress(path.read_bytes()))
STAKE = 10.0
model = [r for r in rows if r["rule"] == "model" and r.get("hr") and r["hr"].get("drift") is not None]
print(f"{len(model)} model buys with an hour reading (of {sum(1 for r in rows if r['rule'] == 'model')} model buys), {len({r['ticker'] for r in model})} markets")


def stats(g):
    n = len(g)
    if n == 0:
        return None
    win = sum(1 for r in g if r["won"]) / n
    ask = sum(r["price"] for r in g) / n
    per = [r["net"] / STAKE for r in g]
    m = sum(per) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in per) / max(1, n - 1))
    return n, win, ask, m, sd / math.sqrt(n)


ds = sorted(r["hr"]["drift"] for r in model)
cuts = [ds[int(len(ds) * q / 5)] for q in range(1, 5)]
labels = ["1 (most against the bet)", "2", "3", "4", "5 (most with the bet)"]
groups = [[] for _ in range(5)]
for r in model:
    k = sum(1 for c in cuts if r["hr"]["drift"] > c)
    groups[k].append(r)
print("cuts (hour drift in typical minutes):", [round(c, 1) for c in cuts])
print(f"{'fifth of the hour drift':26s} {'bets':>5s} {'won':>6s} {'ask':>6s} {'gap':>7s} {'net/$1':>8s} {'+/-':>6s}")
res = []
for lab, g in zip(labels, groups, strict=True):
    n, win, ask, m, se = stats(g)
    res.append((lab, n, win, ask, m, se))
    print(f"{lab:26s} {n:5d} {100 * win:5.1f}% {ask:6.3f} {100 * (win - ask):+6.1f}c {m:+8.3f} {se:6.3f}")
lo, hi = stats(groups[0]), stats(groups[4])
diff = hi[3] - lo[3]
se = math.sqrt(hi[4] ** 2 + lo[4] ** 2)
print(f"net per $1, fifth 5 less fifth 1: {diff:+.3f} +/- {se:.3f} (z {diff / se:+.1f}); won share {100 * hi[1]:.1f}% vs {100 * lo[1]:.1f}%")
against = [r for r in model if r["hr"]["drift"] <= -10]
print(f"bought 10+ typical minutes against the hour: {len(against)} bets, won {100 * stats(against)[1] if against else 0:.1f}%, net/$1 {stats(against)[3] if against else 0:+.3f}" if against else "no bet 10+ typical minutes against the hour")
for series in sorted({r["series"] for r in model}):
    g = [r for r in model if r["series"] == series]
    if len(g) >= 40:
        sub = sorted(g, key=lambda r: r["hr"]["drift"])
        lo5, hi5 = sub[: len(sub) // 3], sub[-(len(sub) // 3):]
        a, b = stats(lo5), stats(hi5)
        print(f"{series:10s} n {len(g):4d}  third most against: won {100 * a[1]:4.1f}% net {a[3]:+.3f} | third most with: won {100 * b[1]:4.1f}% net {b[3]:+.3f}")
if len(sys.argv) > 2:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5))
    xs = range(5)
    ax.bar([x - 0.2 for x in xs], [100 * r[2] for r in res], 0.4, label="won (%)", color="#3b82f6")
    ax.bar([x + 0.2 for x in xs], [100 * r[3] for r in res], 0.4, label="average ask paid (%)", color="#9ca3af")
    for x, r in zip(xs, res, strict=True):
        ax.text(x, 3, f"n={r[1]}\nnet {r[4]:+.3f}/$1", ha="center", fontsize=8, color="white")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(["1 against", "2", "3", "4", "5 with"])
    ax.set_xlabel("the hour's drift in the bet's direction, fifths of this sample")
    ax.set_ylabel("%")
    ax.set_title("PriskP buys: how often the side won, against the price paid, by the hour's drift")
    ax.legend()
    fig.tight_layout()
    fig.savefig(sys.argv[2], dpi=160)
    print("chart", sys.argv[2])
