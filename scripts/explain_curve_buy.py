"""What the curve model saw when it bought (2026-11-08, user: "where did it weigh the hour outlook against the target and decide that Yes was a good choice?").

Given a ticker side and a time, rebuild the 18 readings the live model reads (slope and variability of the spot's cushion to the strike at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes) from the tape, and print each reading, the model's weight on it, and what it adds to the predicted net per $1 (weight x the reading's distance from the training average, in training standard deviations), largest first, plus the ask's own term and the intercept. Read-only.
    scripts/cloud.sh run scripts/explain_curve_buy.py TICKER=KXHYPE15M-26NOV081030-30 SIDE=yes AT=1794151273
"""

import json
import math
import os
from pathlib import Path

from kalshi_moonshot import config, curvemodel, db, variants
from kalshi_moonshot.store import Store

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ticker, side, at = os.environ["TICKER"], os.environ.get("SIDE", "yes"), float(os.environ["AT"])
ASK = float(os.environ.get("ASK", "0.40"))
w = st.db.execute("SELECT strike, close_ts FROM windows WHERE ticker=?", (ticker,)).fetchone()
strike = float(w["strike"]) if w and w["strike"] else float(os.environ["STRIKE"])
ix = variants.crypto_index(ticker.split("-")[0])
G0 = int(at - 60 * 55 - 120)
r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, G0 - 60, at + 5)).fetchall()
ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
g, j, last = [], 0, None
for t in range(G0, int(at) + 1):
    while j < len(ts) and ts[j] <= t:
        last = vs[j]
        j += 1
    g.append(math.log(last) if last else None)
sq = [0.0]
for a, b in zip(g, g[1:], strict=False):
    sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
k = len(g) - 1
read = curvemodel.features(g, sq, k, math.log(strike), 1 if side == "yes" else -1)
model = json.loads((Path(s.data_dir) / "curve_model.json").read_text())
print("index", ix, "strike", strike, "spot at the buy", round(math.exp(g[k]), 4), "cushion bp", round((g[k] - math.log(strike)) * (1 if side == "yes" else -1) * 1e4, 2), "model fitted", model.get("built_at"), "n", model.get("n"))
if read is None:
    print("the curve cannot be read at that time")
    raise SystemExit
names = model["names"]
x = [*read[: len(curvemodel.READ_NAMES)], ASK]
rows = []
total = model["w"][0]
for i, nm in enumerate(names):
    zx = (x[i] - model["mu"][i]) / model["sd"][i]
    c = model["w"][i + 1] * zx
    total += c
    rows.append((c, nm, x[i], zx, model["w"][i + 1]))
print(f"predicted net per $1 {total:+.3f} (the bar: top 20% from {model['cuts'].get('0.2', float('nan')):+.3f}); intercept {model['w'][0]:+.3f}")
print("largest contributions (reading, in training standard deviations from the average, weight, adds to the prediction):")
for c, nm, xv, zx, wt in sorted(rows, key=lambda r_: -abs(r_[0]))[:10]:
    print(f"   {nm:12s} reading {xv:+8.3f}  z {zx:+6.2f}  weight {wt:+8.4f}  adds {c:+.4f}")
print("by look-back (slope + variability added together):")
for m in curvemodel.FIB:
    c = sum(r_[0] for r_ in rows if r_[1].endswith(f"_{m}m"))
    print(f"   {m:3d} min   {c:+.4f}")
print("DONE")
