"""Was the bet bought INSIDE a leg of the curve (a dip for a NO, a rise for a YES) that had not turned yet? (2026-11-25; user: "why can't it walk the curve back and identify it was inside a dip?")
QUESTION (one): on the live priskp bets of the 24 hours to 11-25 10:20 ET, walk the spot back from the buy to the start of its 15 minute window with the wave lane's own zig-zag (waveturn.reading: swings of 0.5 typical minutes on the 30 s median, the pre-open hour's typical minute as the unit; no crossings, no gate) and classify the bet at the buy: INSIDE the leg (the current swing runs in the bet's winning direction, is at least 0.5 u and has given back less than 0.2 u from its extreme: not turned), TURNED AGAINST it (that swing has given back 0.2 u or more), AGAINST (the current swing runs against the bet's side), or NONE (no swing yet, or under 0.5 u). n, lost, as-run net by class, and the INSIDE class split by the R^2 of a straight line over the 30 minutes (trend at or over the median R^2, wave under it).
HONEST LIMITS: 131 bets in 24 hours, one regime; the swing size, reversal and give-back are the wave_turn study's (chosen on the user's marked buys), not retuned; 10 s spot averages carried to a 1 s grid; 'as run' mixes the lane's exits with the entry; a lead only. A rule on it goes to shadow first.
INPUT: bets24.json, targets24.json, d_<index>.raw in the directory given.
Run on this Mac: uv run python scripts/priskp_leg_study.py <dir>"""
import bisect
import json
import re
import statistics
import sys

from kalshi_moonshot import waveturn
from kalshi_moonshot.wavetop import grid

D = sys.argv[1]
IDX = {"KXBNB15M": "BNBUSD_RTI", "KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXHYPE15M": "HYPEUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI", "KXZEC15M": "ZECUSD_RTI", "KXNEAR15M": "NEARUSD_RTI"}


def fee(p):
    return 0.07 * p * (1 - p)


def load(ix):
    line = [x.strip() for x in open(f"{D}/d_{ix}.raw").read().split("\n") if re.match(r"^\d+:[\d.]+,", x.strip())][0]
    t, v = [], []
    for a, b in (p.split(":") for p in line.split(",")):
        t.append(float(a))
        v.append(float(b))
    return t, v


spot = {ix: load(ix) for ix in set(IDX.values())}
targets = json.load(open(f"{D}/targets24.json"))
rows = []
for b in json.load(open(f"{D}/bets24.json")):
    ix = IDX.get(b["ticker"].split("-")[0])
    K = (targets.get(b["ticker"]) or [None])[0]
    if ix is None or not K:
        continue
    T, V = spot[ix]
    op = int(b["ts"] // 900) * 900
    now = int(b["ts"])
    pre = grid(T, V, op - 3600, op)
    win = grid(T, V, op, now + 1)
    if not pre or None in pre or len(pre) < 3600 or not win or None in win or now - op < 30:
        continue
    h = waveturn.hour_of(pre)
    if h.u is None:
        continue
    r = waveturn.reading(win, now - op, K, h.u)
    want = -1 if b["side"] == "no" else 1  # the swing direction that runs in the bet's winning direction
    if r.d == 0 or r.swing_u < waveturn.SWING:
        cls = "NONE (no swing of 0.5 u yet)"
    elif r.d == -want:
        cls = "AGAINST (the swing runs against the bet's side)"
    elif r.gb_u < waveturn.GB:
        cls = "INSIDE the leg, not turned"
    else:
        cls = "TURNED against the bet (gave back 0.2 u or more)"
    w30 = V[bisect.bisect_left(T, b["ts"] - 1800):bisect.bisect_right(T, b["ts"])]
    mu, sd = statistics.fmean(w30), statistics.pstdev(w30)
    n_ = len(w30)
    xm = (n_ - 1) / 2
    sxx = sum((i - xm) ** 2 for i in range(n_))
    sl = sum((i - xm) * (y - mu) for i, y in enumerate(w30)) / sxx
    ic = mu - sl * xm
    r2 = 1.0 - sum((y - (ic + sl * i)) ** 2 for i, y in enumerate(w30)) / (sd * sd * n_) if sd > 0 else 0.0
    pay, n = float(b["pay"]), float(b["n"])
    cost = pay + fee(pay)
    got = (float(b["xp"]) - fee(float(b["xp"]))) if b["xts"] else (1.0 if b["out"] == b["side"] else 0.0)
    rows.append({"tk": b["ticker"], "ts": b["ts"], "stake": pay * n, "side": b["side"], "cls": cls, "r2": r2, "swing": r.swing_u, "gb": r.gb_u, "net": (got - cost) * n, "pc": got - cost})


def line(label, g):
    if not g:
        print(f"{label:60s} n   0")
        return
    lost = sum(1 for x in g if x["pc"] <= 0)
    print(f"{label:60s} n {len(g):3d}  as run ${sum(x['net'] for x in g):+8.2f}  lost {lost:3d} ({100 * lost / len(g):.0f}%)  per contract {sum(x['pc'] for x in g) / len(g):+.3f}")


med = statistics.median(x["r2"] for x in rows)
print(f"{len(rows)} bets; median R^2 {med:.2f}")
line("all", rows)
for c in sorted({x["cls"] for x in rows}):
    line(c, [x for x in rows if x["cls"] == c])
inside = [x for x in rows if x["cls"].startswith("INSIDE")]
print("\nINSIDE the leg, by the 30 minute straight-line fit:")
line("  trend (R^2 at or over the median): the rule would leave these", [x for x in inside if x["r2"] >= med])
line("  wave (R^2 under the median): the rule would refuse these", [x for x in inside if x["r2"] < med])
print("\nAGAINST the current swing, by the 30 minute straight-line fit (added 2026-11-25 after the first live refusal, NEAR YES 11:48 against a 0.50 u swing at R^2 0.59, which settled YES):")
against = [x for x in rows if x["cls"].startswith("AGAINST")]
line("  trend (R^2 at or over the median)", [x for x in against if x["r2"] >= med])
line("  wave (R^2 under the median)", [x for x in against if x["r2"] < med])
print("\nTHE REFUSED SET (AGAINST or INSIDE, in wave markets) BY SIDE (added 2026-11-25 after BTC YES 12:33 was refused inside a 3.27 u rise and settled YES):")
refused_set = [x for x in rows if (x["cls"].startswith("AGAINST") or x["cls"].startswith("INSIDE")) and x["r2"] < med]
line("  NO bets (a dip: the side bought after a fall)", [x for x in refused_set if x["side"] == "no"])
line("  YES bets (a rise: the side bought after a rise)", [x for x in refused_set if x["side"] == "yes"])
print("\nthe 08:30 round:")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} {x['cls'][:34]:34s} swing {x['swing']:.2f}u given back {x['gb']:.2f}u R^2 {x['r2']:.2f} net ${x['net']:+.2f}")


# ---- the FLIP (user, 2026-11-25: "your numbers don't matter, because those losses likely would have flipped to wins on the opposite buy (how this should pan out)") ----
# For each bet the rule would refuse (AGAINST, or INSIDE an unturned leg in a wavy market), what the OPPOSITE side would have made bought at its ask at that moment (1 - the bet side's stored bid), the same dollar stake, held to the market's own result, fee on the buy. Quotes: the decision's own bid and ask for the bet's side.
quotes = {(q["ticker"], q["ts"] // 5): q for q in json.load(open(f"{D}/quotes24.json"))}
flips = []
for x in rows:
    if not (x["cls"].startswith("AGAINST") or (x["cls"].startswith("INSIDE") and x["r2"] < med)):
        continue
    q = quotes.get((x["tk"], int(x["ts"]) // 5)) or quotes.get((x["tk"], int(x["ts"]) // 5 - 1)) or quotes.get((x["tk"], int(x["ts"]) // 5 + 1))
    res = (targets.get(x["tk"]) or [None, None])[1]
    if q is None or not res or q["bid"] is None:
        continue
    opp = "yes" if x["side"] == "no" else "no"
    ask = 1.0 - float(q["bid"])
    if not (0.02 < ask < 0.98):
        continue
    net = ((1.0 if res == opp else 0.0) - ask - fee(ask)) * x["stake"] / ask
    flips.append({**x, "opp": opp, "ask": ask, "flip": net, "won": res == opp})
print(f"\nFLIP of the {len(flips)} refused bets with a quote and a result (the opposite side at its ask, the bet's own stake, held to the result):")
print(f"  as run (the refused bets): ${sum(x['net'] for x in flips):+.2f}")
print(f"  the opposite side bought instead: ${sum(x['flip'] for x in flips):+.2f}  ({sum(1 for x in flips if x['won'])} won, {sum(1 for x in flips if not x['won'])} lost)")
for lo, hi, label in ((0.0, 0.55, "opposite ask under 0.55 (the cheap side: the wave lane's price range)"), (0.55, 0.85, "opposite ask 0.55 to 0.85 (PriskP's own band)"), (0.85, 1.0, "opposite ask over 0.85")):
    g = [x for x in flips if lo <= x["ask"] < hi]
    if g:
        print(f"  {label}: n {len(g)}, as run ${sum(x['net'] for x in g):+.2f}, flipped ${sum(x['flip'] for x in g):+.2f}, {sum(1 for x in g if x['won'])} won")
print("  the 08:30 round:")
for x in flips:
    if "26NOV250830" in x["tk"]:
        print(f"    {x['tk'][:24]:24s} bet {x['side'].upper()} ${x['net']:+.2f} -> {x['opp'].upper()} at {x['ask']:.2f} ${x['flip']:+.2f}")
