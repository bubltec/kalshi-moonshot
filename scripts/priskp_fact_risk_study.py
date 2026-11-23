"""Which readings carry RISK for PriskP's buys? (2026-11-23; user: the fact board should weigh different facts toward the risk; "our score is not a price comparison. It's a risk"; rule of 2026-11-21: the score affects the price, it is never compared to it, and is not judged held to settlement as a profit.)
QUESTION (one): for each reading the PriskP run recorded at a buy (priskp_run.py: the five checks, the volatility reading, the spot's features at 15/30/60 minutes, the price's own slope and curvature, the hour profile, the minute) plus the closing speed and the distance to the target read from the bet's own chart, does the share of buys that END ON THE WRONG SIDE differ between the third of buys with the lowest reading and the third with the highest, WITHIN THE SAME ASK BAND? Within a band the price level is held the same, so the reading is judged as a risk and not as an echo of the price. Bands are the lane's own range cut in three (0.55-0.65, 0.65-0.75, 0.75-0.85; the model also bought outside it, those are left out). A reading counts when its three bands agree in sign and the combined z (the three bands' z's added and divided by sqrt 3) is 3.3 or more in size: about 45 readings are tested, so 2 would pass by chance at the usual 2, and 3.3 is the cut for about one chance in a thousand each (a stated choice).
NOT a profit number: no exit, no fee, no held-to-settlement net. A reading that separates buys that end wrong from buys that end right is a candidate for a weight; it is not yet a rule (shadow first).
Run: .venv/bin/python scripts/priskp_fact_risk_study.py PATH/priskp_buys_crypto.json.gz"""
import gzip
import json
import math
import sys

rows = json.loads(gzip.decompress(open(sys.argv[1], "rb").read()))
m = [r for r in rows if r["rule"] == "model" and 0.55 <= r["price"] < 0.85]
print(f"{len(m)} model buys inside the lane's 0.55-0.85 range, {sum(1 for r in m if not r['won'])} ended on the wrong side ({100 * sum(1 for r in m if not r['won']) / len(m):.1f}%)")


def features(r):
    f = {}
    for k, v in r["raw"].items():
        if v is not None:
            f["raw:" + k] = v
    for k in ("cross", "hi_ago", "lo_ago", "range", "drift", "eff"):
        if r["hr"].get(k) is not None:
            f["hour:" + k] = r["hr"][k]
    f["minute"] = r["minute"]
    idx = r["chart"]["idx"]
    i = int(round((3600.0 + r["minute"] * 60.0) / 10.0))
    sg = 1.0 if r["side"] == "yes" else -1.0
    u = r["hr"].get("u15") or 0.0
    if i < len(idx) and i >= 6 and idx[i] is not None and idx[i - 6] is not None and u > 0:
        f["chart:distance_now_u"] = sg * idx[i] / 100.0 / u  # the cushion in log units over the typical minute
        f["chart:closing_60s_u"] = -(sg * (idx[i] - idx[i - 6]) / 100.0) / u  # positive = the cushion is shrinking, in typical minutes per minute
        if idx[i - 3] is not None:
            f["chart:closing_30s_u"] = -(sg * (idx[i] - idx[i - 3]) / 100.0) / (u * 0.5)
    return f


for r in m:
    r["_f"] = features(r)
names = sorted({k for r in m for k in r["_f"]})
bands = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85)]
results = []
for nm in names:
    zs, diffs, lows, highs = [], [], 0, 0
    ok = True
    pooled_lo = pooled_hi = []
    lo_all, hi_all = [], []
    for lo, hi in bands:
        g = [r for r in m if lo <= r["price"] < hi and nm in r["_f"]]
        if len(g) < 300:
            ok = False
            break
        g.sort(key=lambda r: r["_f"][nm])
        t = len(g) // 3
        a, b = g[:t], g[-t:]
        pa = sum(1 for r in a if not r["won"]) / len(a)
        pb = sum(1 for r in b if not r["won"]) / len(b)
        se = math.sqrt(pa * (1 - pa) / len(a) + pb * (1 - pb) / len(b))
        zs.append((pa - pb) / se if se > 0 else 0.0)
        diffs.append(pa - pb)
        lo_all += a
        hi_all += b
    if not ok:
        continue
    Z = sum(zs) / math.sqrt(len(zs))
    same = all(d > 0 for d in diffs) or all(d < 0 for d in diffs)
    pl = sum(1 for r in lo_all if not r["won"]) / len(lo_all)
    ph = sum(1 for r in hi_all if not r["won"]) / len(hi_all)
    results.append((abs(Z), nm, Z, same, diffs, pl, ph))
results.sort(reverse=True)
print(f"{len(results)} readings tested. Z > 0 = the LOW third ends wrong more often than the HIGH third. 'agree' = all three bands the same sign.")
print(f"{'reading':22s} {'Z':>6s} {'agree':>6s} {'lost: low third':>16s} {'high third':>11s}   by band (low-high, pts)")
for _, nm, Z, same, diffs, pl, ph in results[:60]:
    flag = "  <== passes" if same and abs(Z) >= 3.3 else ""
    print(f"{nm:22s} {Z:+6.1f} {str(same):>6s} {100 * pl:15.1f}% {100 * ph:10.1f}%   " + " ".join(f"{100 * d:+5.1f}" for d in diffs) + flag)
print("passes:", [nm for _, nm, Z, same, *_ in results if same and abs(Z) >= 3.3])
