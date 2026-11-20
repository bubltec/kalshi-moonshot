"""Print the binary risk model's fitted weights (user, 2026-11-20: "what are the weights here?"). The same fit the hourly refit makes (binaryrisk.build_rows over the last 24 h of settled markets, coins and commodities, S&P out, then binaryrisk.fit), once, on the runner's newest backup. Prints, for each of the five exits, the weight on every standardised reading (a weight is the change in predicted net per $1 for a one standard deviation move of that reading; the constant is the mean), each reading's mean and standard deviation, and the bars. Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_weights.py"""
import time
from kalshi_moonshot import binaryrisk as br, config

s = config.reload()
t0 = time.time()
rows = br.build_rows(s, max(r for r in [time.time()]))
print(f"{len(rows)} training sides, {time.time() - t0:.0f}s")
m = br.fit(rows)
print(f"bars {m['cuts']}\n")
print(f"{'reading':14s} {'mean':>9s} {'sd':>8s} | " + " ".join(f"{e:>8s}" for e in m["exits"]))
print(f"{'(constant)':14s} {'':>9s} {'':>8s} | " + " ".join(f"{w[0]:+8.3f}" for w in m["W"]))
for i, n in enumerate(m["names"]):
    print(f"{n:14s} {m['mu'][i]:9.3f} {m['sd'][i]:8.3f} | " + " ".join(f"{w[i + 1]:+8.3f}" for w in m["W"]))
