"""WHAT THE PERP'S CHANCE AS A LEVEL WOULD HAVE DONE on the bot's own 132 live commodity bets with step notes (2026-11-21). The bar for a bet is the stated quantile (the median, 0.5) of its series' own perp-chance readings in the 24 hours before the bet (perpchance.quantile_bar on scripts/perp_chance_history.py's readings: every market, every 10 s, sides priced 0.60-0.92). The bet's own chance is the one its step recorded. A bet under its bar is refused; a bet with no bar (short history) is decided by the old gap rule, which passed it.
Reported: the bets refused and kept, their dollars, ahead and behind; the same at 0.8 and 0.9, added AFTER the median was seen to refuse nothing (every bet's chance is already over its series' median: the lane's other steps pick high chances), so those two rows are chosen after seeing results and are not a test.
    PYTHONPATH=src:scripts python3 scripts/own_bets_perp_level.py"""
import glob, gzip, json, sys
sys.argv = sys.argv[:1]
from own_bets_table import B
from kalshi_moonshot import perpchance

H = json.load(gzip.open(sorted(glob.glob("job-results/*/files/perp_chance_history.json.gz"))[-1]))
HT = {k: ([x[0] for x in v], [x[1] for x in v]) for k, v in H.items()}
print("readings per series:", {k: len(v) for k, v in sorted(H.items())})
tot = lambda g: "n %3d, %+7.2f$, ahead %3d, behind %2d" % (len(g), sum(r["net"] for r, _ in g), sum(1 for r, _ in g if r["net"] > 0), sum(1 for r, _ in g if r["net"] <= 0))
for q in (0.5, 0.8, 0.9):
    ref, keep, nobar, bars = [], [], [], []
    for r, p in B:
        t, v = HT.get(r["series"], ([], []))
        bar, n = perpchance.quantile_bar(t, v, q, r["ts"] - 9.0)
        if bar is None: nobar.append((r, p)); continue
        bars.append(bar)
        (keep if p["perp_chance"] >= bar else ref).append((r, p))
    print(f"\nquantile {q:.2f}{' (the stated one)' if q == 0.5 else ' (added after seeing the median refuse nothing)'}: bars from {min(bars):.3f} to {max(bars):.3f}, median {sorted(bars)[len(bars) // 2]:.3f}")
    print("  all bets            : " + tot(B))
    print("  refused (under bar) : " + tot(ref))
    print("  bought (at or over) : " + tot(keep))
    print("  no bar, gap rule    : " + tot(nobar))
    if q == 0.5:
        for r, p in sorted(ref, key=lambda x: x[0]["net"])[:10]:
            print("    refused %-30s %-3s paid %.2f net %+6.2f chance %.3f gap %+.3f" % (r["ticker"], r["side"], r["paid"], r["net"], p["perp_chance"], p["perp_less_ask"]))
