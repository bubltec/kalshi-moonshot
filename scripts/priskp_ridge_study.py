"""Does a RIDGE (a staircase down: drops with flat shelves between them, never recovering) in the hour before the buy mark the PriskP buys that lose? (2026-11-24; user, on DOGE KXDOGE15M-26NOV240815: "this was a ridge (flat in between drops) ... another indicator we shouldn't buy YES"; and "you're looking at averages and not where it matters")
QUESTION (one): on the same model buys as scripts/priskp_thin_cushion_study.py (priskp_run.py rule "model"), do the buys made INTO a ridge win less than their price implies, and do the ones that lost carry the ridge more often than the ones that won?
THE READING (stated before the run; no setting is fitted): hr["bins"] = the hour before the buy in twelve 5-minute averages of the settlement index against the strike, in the bet's direction, in the market's own typical minute u15 (priskp_run.hour_profile). Between neighbouring bins, a FALL is a step of at least 1 typical minute down (the bins are already in typical minutes, so no price unit), a SHELF is a step within 1 typical minute either way. RIDGE = at least 2 falls and at least 2 shelves, the hour net against the bet by more than 3 typical minutes (bins[-1] - bins[0] < -3), and the last bin within 1 typical minute of the hour's lowest (no recovery). The 1 and 3 are my choice, not scored on these bets; the fall and shelf size is 1 typical minute because that is the unit.
Per group: bets, share won, average ask, gap (won less ask), net per $1 after the fee with its error. A look at the bets that lost: how many of the losers had a ridge against how many of the winners. A lead only, read-only.
Run: .venv/bin/python scripts/priskp_ridge_study.py PATH/priskp_buys_crypto.json.gz"""
import gzip
import json
import math
import sys

rows = json.loads(gzip.decompress(open(sys.argv[1], "rb").read()))
model = [r for r in rows if r["rule"] == "model" and r.get("hr") and r["hr"].get("bins") and r["raw"].get("cushion") is not None]
print(f"{len(model)} model buys with an hour reading")


def ridge(bins):
    d = [b - a for a, b in zip(bins, bins[1:], strict=False)]
    falls = sum(1 for x in d if x <= -1.0)
    shelves = sum(1 for x in d if abs(x) < 1.0)
    return falls >= 2 and shelves >= 2 and bins[-1] - bins[0] < -3.0 and bins[-1] - min(bins) <= 1.0


def st(g):
    n = len(g)
    if n < 2:
        return None
    per = [r["net"] / 10.0 for r in g]
    m = sum(per) / n
    se = math.sqrt(sum((x - m) ** 2 for x in per) / (n - 1) / n)
    return n, sum(r["won"] for r in g) / n, sum(r["price"] for r in g) / n, m, se


def show(lab, g):
    s = st(g)
    print(f"{lab:38s} " + ("(too few)" if not s else f"{s[0]:5d} {100 * s[1]:5.1f}% {s[2]:6.3f} {100 * (s[1] - s[2]):+6.1f}c {s[3]:+8.3f} {s[4]:6.3f}"))


for r in model:
    r["ridge"] = ridge(r["hr"]["bins"])
thin = [r for r in model if r["raw"]["cushion"] <= 0.213]
print(f"{'group':38s} {'bets':>5s} {'won':>6s} {'ask':>6s} {'gap':>7s} {'net/$1':>8s} {'+/-':>6s}")
show("every buy, ridge", [r for r in model if r["ridge"]])
show("every buy, no ridge", [r for r in model if not r["ridge"]])
show("thin cushion (third), ridge", [r for r in thin if r["ridge"]])
show("thin cushion (third), no ridge", [r for r in thin if not r["ridge"]])
show("thin and before minute 5, ridge", [r for r in thin if r["ridge"] and r["minute"] < 5])
show("thin and before minute 5, no ridge", [r for r in thin if not r["ridge"] and r["minute"] < 5])
show("not thin, ridge", [r for r in model if not r["raw"]["cushion"] <= 0.213 and r["ridge"]])
lost = [r for r in model if not r["won"]]
won = [r for r in model if r["won"]]
print(f"ridge among the {len(lost)} lost buys: {100 * sum(r['ridge'] for r in lost) / len(lost):.1f}%; among the {len(won)} won buys: {100 * sum(r['ridge'] for r in won) / len(won):.1f}%")
tl = [r for r in thin if not r["won"] and r["minute"] < 5]
tw = [r for r in thin if r["won"] and r["minute"] < 5]
print(f"thin and early: ridge among {len(tl)} lost {100 * sum(r['ridge'] for r in tl) / len(tl):.1f}%, among {len(tw)} won {100 * sum(r['ridge'] for r in tw) / len(tw):.1f}%")
print("by series, ridge buys (n >= 25):")
for sr in sorted({r["series"] for r in model}):
    g = [r for r in model if r["series"] == sr and r["ridge"]]
    if len(g) >= 25:
        show("  " + sr, g)

# the DOGE bet, from the recorded index (minute averages 07:03-08:03 ET, strike 0.0555, typical minute 5e-5 as the log said), for the same reading
dg = [0.055819, 0.055821, 0.055783, 0.055761, 0.055764, 0.055746, 0.055723, 0.055728, 0.055725, 0.055691, 0.055641, 0.055639, 0.055624, 0.055622, 0.055649, 0.055641, 0.055666, 0.055678, 0.055656, 0.055689, 0.055678, 0.055634, 0.055607, 0.05561, 0.055591, 0.055588, 0.055599, 0.055593, 0.055642, 0.05563, 0.055571, 0.055523, 0.055514, 0.055535, 0.055611, 0.055645, 0.055533, 0.055479, 0.055559, 0.055601, 0.055624, 0.055676, 0.055686, 0.055645, 0.055641, 0.055625, 0.055661, 0.0556, 0.055566, 0.05556, 0.05561, 0.055587, 0.055571, 0.055562, 0.055511, 0.055523, 0.055515, 0.055528, 0.055572, 0.055597, 0.055561]
u = 0.00005
b5 = [(sum(dg[5 * i:5 * i + 5]) / 5 - 0.0555) / u for i in range(12)]
print("DOGE 08:03 bins (typical minutes, YES side):", [round(x, 1) for x in b5], "ridge:", ridge(b5))
