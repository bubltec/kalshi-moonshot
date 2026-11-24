"""Do PriskP buys with a thin cushion, early in the window, lose to their price? (2026-11-24; user, on DOGE KXDOGE15M-26NOV240815: "it bought yes with like no cushion, early", "should have bought the opposite")
QUESTION (one): on every buy the PriskP checks model made across the tape (scripts/priskp_run.py, rule "model": five checks passing, minutes 2-14, no ask of 0.45-0.55, a take at the ask, held to settlement, fee 0.07 p (1-p) on the buy), do the buys with the lowest cushion that were made early in the window win less than their price implies?
READINGS: cushion = the checks' own reading, raw["cushion"], the blended 1/3/5-minute average spot against the strike in the market's own typical minute u15 x sqrt(minutes left) (binaryrisk), in the bet's direction; minute = minutes into the window at the buy.
CUTS (said so: chosen from this sample, nothing set): the cushion in thirds of this sample; the minute in three bands, 2-5, 5-9, 9-14 (my choice before seeing results). Per cell: bets, share won, average ask paid, gap (won less ask: the edge before the fee), net per $1 after the fee with its standard error. A cell counts with its error in view. A lead only: read-only, no rule proposed.
Run: .venv/bin/python scripts/priskp_thin_cushion_study.py PATH/priskp_buys_crypto.json.gz"""
import gzip
import json
import math
import sys

rows = json.loads(gzip.decompress(open(sys.argv[1], "rb").read()))
STAKE = 10.0
model = [r for r in rows if r["rule"] == "model" and r["raw"].get("cushion") is not None]
print(f"{len(model)} model buys, {len({r['ticker'] for r in model})} markets")


def stat(g):
    n = len(g)
    if n < 2:
        return None
    win = sum(r["won"] for r in g) / n
    ask = sum(r["price"] for r in g) / n
    per = [r["net"] / STAKE for r in g]
    m = sum(per) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in per) / (n - 1))
    return n, win, ask, m, sd / math.sqrt(n)


def line(lab, g):
    s = stat(g)
    if not s:
        return f"{lab:34s} (too few)"
    n, win, ask, m, se = s
    return f"{lab:34s} {n:5d} {100 * win:5.1f}% {ask:6.3f} {100 * (win - ask):+6.1f}c {m:+8.3f} {se:6.3f}"


cs = sorted(r["raw"]["cushion"] for r in model)
cuts = [cs[len(cs) // 3], cs[2 * len(cs) // 3]]
print("cushion cuts (thirds of the sample):", [round(c, 3) for c in cuts], " range", round(cs[0], 2), "to", round(cs[-1], 2))
third = lambda r: sum(r["raw"]["cushion"] > c for c in cuts)  # noqa: E731
bands = [(2, 5), (5, 9), (9, 14.01)]
head = f"{'cell':34s} {'bets':>5s} {'won':>6s} {'ask':>6s} {'gap':>7s} {'net/$1':>8s} {'+/-':>6s}"
print(head)
for k, name in enumerate(["thin third", "middle third", "thick third"]):
    print(line(f"{name}, all minutes", [r for r in model if third(r) == k]))
print()
for a, b in bands:
    for k, name in enumerate(["thin", "middle", "thick"]):
        print(line(f"minute {a}-{int(b)}, {name}", [r for r in model if a <= r["minute"] < b and third(r) == k]))
    print()
thin_early = [r for r in model if r["minute"] < 5 and third(r) == 0]
print(line("thin AND minute 2-5", thin_early))
print(line("every other buy", [r for r in model if not (r["minute"] < 5 and third(r) == 0)]))
print("by series (thin & early):")
for sr in sorted({r["series"] for r in thin_early}):
    g = [r for r in thin_early if r["series"] == sr]
    if len(g) >= 15:
        print(line("  " + sr, g))
print("opposite side (hindsight check, same buys): the other side won on", sum(1 for r in thin_early if not r["won"]), "of", len(thin_early), "thin-early buys")
