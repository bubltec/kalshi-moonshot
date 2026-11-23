"""Question (user, 2026-11-22, after two live commodity NO bets lost to a metals bounce: "it's betting the wrong side; maybe we should open up lower prices"): held to settlement, what does each ask band pay on commodity sides inside the lane's entry window?
Same bets as commodity_cushion_perp_bar.py but the price band is opened to 0.05-0.95 (spread <= 0.035, minutes 10-15, first passing moment per side per market on a 30 s grid), fee on the buy, the perp's z read as the live step does.
A read of the bands the lane does not buy, not a rule. Held to settlement only: it ignores the lane's exits. Usage: .venv/bin/python scripts/commodity_price_band_open.py job-results/20261121-154853-21423"""
import bisect, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import study_cache as sc
from kalshi_moonshot import variants
from kalshi_moonshot.strategy import fee_per_contract as fee
cache = sc.Cache(sys.argv[1]); rows = []
for w in cache.windows:
    if w["kind"] != "commodity" or not w["strike"] or w["series"].startswith("KXINX"): continue
    T, V = cache.ticks(w["index"]); qt, qb, qa = cache.quotes(w); close = w["close"]; opened = close - 900.0
    for side in ("yes", "no"):
        for el in range(600, 886, 30):
            t = opened + el; j = bisect.bisect_right(qt, t) - 1
            if j < 0 or t - qt[j] > 10 or qb[j] <= 0 or qa[j] - qb[j] > 0.035: continue
            ask = qa[j] if side == "yes" else 1.0 - qb[j]
            if not 0.05 <= ask <= 0.95: continue
            snap = variants.snapshot(T, V, w["strike"], side, t, close)
            if snap is None: break
            rows.append((snap["z"], ask, 1.0 if (w["result"] == "yes") == (side == "yes") else 0.0)); break
def rep(name, sel):
    if not sel: print(f"{name:>16}: n=0"); return
    st = sum(r[1] + fee(r[1]) for r in sel); g = sum(r[2] for r in sel)
    print(f"{name:>16}: n={len(sel):5d} won {g/len(sel):5.1%} avg ask {sum(r[1] for r in sel)/len(sel):.3f} net ${g-st:8.2f} on ${st:8.2f} = {(g-st)/st:+.3f} per $")
print(len(rows), "sides")
for lo, hi in [(.05,.2),(.2,.35),(.35,.5),(.5,.64),(.64,.78),(.78,.92),(.92,.96)]:
    rep(f"ask {lo}-{hi}", [r for r in rows if lo <= r[1] < hi])
print("-- by the perp's cushion z (the curve shape the live step reads), all asks --")
for lo, hi in [(-99,0),(0,.8),(.8,1.5),(1.5,99)]:
    rep(f"z {lo:g}..{hi:g}", [r for r in rows if lo <= r[0] < hi])
