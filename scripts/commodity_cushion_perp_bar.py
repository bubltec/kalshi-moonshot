"""Question (user, 2026-11-22): the commodity cushion step now reads Kalshi's perp reference against its own open tick, but min_cushion_z = 0.8 was set on BiQuote readings. Where does 0.8 sit on the perp's z?
Same bets: the first moment (30 s grid, minutes 10-15 of the window = the lane's entry_elapsed) a side is inside the lane's price band 0.64-0.92 and spread <= 0.035, z read exactly as the live step does
(variants.snapshot on the perp, strike = the perp's open tick). Held to settlement, fee on the buy. Not a rule; a read of the bar. Setting values are the lane's, chosen before this look.
Usage: .venv/bin/python scripts/commodity_cushion_perp_bar.py job-results/20261121-154853-21423"""
import bisect, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc
from kalshi_moonshot import variants
from kalshi_moonshot.strategy import fee_per_contract as fee

cache = sc.Cache(sys.argv[1])
rows = []
for w in cache.windows:
    if w["kind"] != "commodity" or not w["strike"] or w["series"].startswith("KXINX"): continue
    T, V = cache.ticks(w["index"]); qt, qb, qa = cache.quotes(w)
    close = w["close"]; opened = close - 900.0
    for side in ("yes", "no"):
        for el in range(600, 886, 30):
            t = opened + el
            j = bisect.bisect_right(qt, t) - 1
            if j < 0 or t - qt[j] > 10: continue
            ask = qa[j] if side == "yes" else 1.0 - qb[j]
            if not (0.64 <= ask <= 0.92 and qa[j] - qb[j] <= 0.035 and qb[j] > 0): continue
            snap = variants.snapshot(T, V, w["strike"], side, t, close)
            if snap is None: break
            won = (w["result"] == "yes") == (side == "yes")
            rows.append((snap["z"], ask, 1.0 if won else 0.0, w["ticker"], side, t, w["series"]))
            break
print(f"{len(rows)} first-passing sides, {len({r[3] for r in rows})} markets, windows since {cache.meta['from']}")
def rep(name, sel):
    n = len(sel)
    if not n: print(f"{name:>14}: n=0"); return
    stake = sum(r[1] + fee(r[1]) for r in sel); get = sum(r[2] for r in sel)
    print(f"{name:>14}: n={n:5d} won {sum(r[2] for r in sel)/n:5.1%} avg ask {sum(r[1] for r in sel)/n:.3f} net ${get-stake:8.2f} on ${stake:8.2f} = {(get-stake)/stake:+.3f} per $")
bins = [(-99, 0), (0, 0.4), (0.4, 0.8), (0.8, 1.2), (1.2, 1.8), (1.8, 99)]
for lo, hi in bins: rep(f"z {lo:g}..{hi:g}", [r for r in rows if lo <= r[0] < hi])
rep("z >= 0.8 (live)", [r for r in rows if r[0] >= 0.8]); rep("z < 0.8 (refused)", [r for r in rows if r[0] < 0.8]); rep("all", rows)
zs = sorted(r[0] for r in rows); q = lambda p: zs[int(p * (len(zs) - 1))]
print("z quantiles 10/25/50/75/90:", [round(q(p), 2) for p in (.1, .25, .5, .75, .9)], "| share under 0.8:", f"{sum(z < 0.8 for z in zs)/len(zs):.1%}")
# the bar by halves of the sample's time is not a split of the rule (CLAUDE.md 4); by weeks only to show drift of the number
