"""The live curve model's weights, per look-back (2026-11-08, user: "how is each level of the history weighted?").

Prints the fitted ridge: for each of the 18 readings (slope and variability at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes) and the ask, the training average, the training standard deviation and the weight on the reading after it is standardised ((reading - average) / standard deviation), i.e. the predicted net per $1 added by one standard deviation of that reading. Read-only.
    scripts/cloud.sh run scripts/curve_model_weights.py
"""

import json
import time
from pathlib import Path

from kalshi_moonshot import config, curvemodel

s = config.reload()
m = json.loads((Path(s.data_dir) / "curve_model.json").read_text())
print(f"fitted {time.strftime('%Y-%m-%d %H:%M', time.gmtime(m['built_at']))} UTC on {m['n']} moments of the last {m['train_days']:g} days (ridge {curvemodel.RIDGE:g}); mean net per $1 of those moments {m['mean_net']:+.3f}; bar for the top 20%: {m['cuts'].get('0.2', float('nan')):+.3f}")
print(f"intercept {m['w'][0]:+.4f}\n")
print(f"{'look-back':>10s} | {'slope weight':>13s} {'slope avg':>10s} {'slope sd':>9s} | {'var weight':>11s} {'var avg':>9s} {'var sd':>8s}")
idx = {n: i for i, n in enumerate(m["names"])}
for mins in curvemodel.FIB:
    a, b = idx[f"slope_{mins}m"], idx[f"var_{mins}m"]
    print(f"{mins:>7d} min | {m['w'][a + 1]:+13.4f} {m['mu'][a]:+10.3f} {m['sd'][a]:9.3f} | {m['w'][b + 1]:+11.4f} {m['mu'][b]:9.3f} {m['sd'][b]:8.3f}")
a = idx["ask"]
print(f"{'ask':>10s} | weight {m['w'][a + 1]:+.4f} per standard deviation ({m['sd'][a]:.3f} of price, average {m['mu'][a]:.3f})")
tot = {mins: abs(m["w"][idx[f"slope_{mins}m"] + 1]) + abs(m["w"][idx[f"var_{mins}m"] + 1]) for mins in curvemodel.FIB}
print("\nshare of the total weight size by look-back (|slope| + |variability|):")
z = sum(tot.values())
for mins in curvemodel.FIB:
    print(f"   {mins:3d} min  {100 * tot[mins] / z:5.1f}%")
print("DONE")
