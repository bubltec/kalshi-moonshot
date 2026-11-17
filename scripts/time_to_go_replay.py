"""The curve exit's loss side on the lanes' own live bets: sell a bet when the spot is on the wrong side of its target.

Question (user, 2026-11-17: "now lets. fix the curve exit, because WTI just landed 2 0s"): the two WTI bets (16:30 YES bought at 0.83, 17:45 NO bought at 0.75) rode to nothing with no sale attempted: every loss exit on the commodity lanes was off or in shadow. On the 17:45 bet the spot was on the wrong side of the target for the last 4.7 minutes while the NO bid was still 0.55. The curve exit has a sale for exactly that (time-to-go). On the bets the lanes really made, does selling when the spot is behind remove total losses, at what cost, and how many winners does it sell?
One question on the same bets: every live bet of the commodity lanes and of crypto_multiscale from 2026-11-09 to 11-16 with quotes and index readings on the archive, from its own buy. Held, or sold at the side's bid (the fee on the sale) by one version. The profit take and other exits are left out: the loss side alone.
"Behind": the cushion for the side held (the index against the target, in its typical moves for the time left; read every 30 s) is under the version's level. The sale waits a quarter of the time left with the spot behind at every reading in between.

Versions (fixed before the run):
  behind, any time      under 0 at any point after the buy
  behind, last 4 min    under 0, only in the last 240 s (the time-to-go sale as it was built, curve_behind_last = 240)
  behind, last 2 min    under 0, only in the last 120 s
  behind by 0.5         under -0.5 at any point
  sliding               no cut-off: behind by more than (share of the market left) typical moves. At the close any shortfall counts; with the whole market left it has to be a full typical move behind

    .venv/bin/python scripts/time_to_go_replay.py
"""
import bisect
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import risk_attribute_replay as R  # noqa: E402

from kalshi_moonshot.strategy import fee_per_contract as fee  # noqa: E402

ET = ZoneInfo("America/New_York")
quotes = R._quotes_15m()
wins = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open(f"{R.ROOT}/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
CUSH = defaultdict(list)
for r_ in list(csv.reader(open(R.OUT)))[1:]:
    CUSH[r_[0]].append((int(r_[1]), float(r_[7])))

VERSIONS = {
    "behind, any time": lambda c, left: c < 0,
    "behind, last 4 min": lambda c, left: c < 0 and left <= 240,
    "behind, last 2 min": lambda c, left: c < 0 and left <= 120,
    "behind by 0.5": lambda c, left: c < -0.5,
    "sliding": lambda c, left: c < -(left / 900.0),
}
NAMES = ["hold", *VERSIONS]


def replay(path, lanes=None):
    bets, skipped = [], 0
    for r in csv.reader(open(path)):
        if len(r) < 5 or (lanes and (len(r) < 6 or r[5] not in lanes)):
            continue
        tk, ts, side, paid, stake = r[0], float(r[1]), r[2], float(r[3]), float(r[4])
        if tk not in quotes or tk not in wins or tk not in CUSH:
            skipped += 1
            continue
        close, result = wins[tk]
        qt, qb, qa = quotes[tk]
        marks = [(t, (c if side == "yes" else -c)) for t, c in CUSH[tk] if t > ts]
        won = result == side
        cost = paid + fee(paid)
        held = ((1.0 if won else 0.0) - cost) / cost
        nets, sold = {"hold": held}, {}
        for name, behind in VERSIONS.items():
            since, out = None, None
            for t, c in marks:
                left = close - t
                if left <= 8:
                    break
                if not behind(c, left):
                    since = None
                    continue
                if since is None:
                    since = t
                if t - since >= 0.25 * left - 1e-9:
                    j = bisect.bisect_right(qt, t) - 1
                    if j >= 0:
                        out = float(qb[j] if side == "yes" else 1.0 - qa[j])
                    break
            if out is None:
                nets[name] = held
            else:
                s = min(max(out, 0.01), 0.99)
                nets[name] = (s - fee(s) - cost) / cost
                sold[name] = True
        bets.append(dict(nets=nets, sold=sold, won=won, stake=stake, day=datetime.datetime.fromtimestamp(ts, ET).strftime("%m-%d")))
    return bets, skipped


def table(title, bets, skipped):
    if not bets:
        return
    print(f"{title}: {len(bets)} bets ({skipped} without quotes, readings or a result), ${sum(b['stake'] for b in bets):.0f} staked, {sum(b['won'] for b in bets)} won at settlement")
    print(f"  {'version':20s} {'sold':>5s} {'of them winners':>16s} {'total losses':>13s} {'cents per $':>12s} {'as staked':>11s} {'against hold':>13s} {'days ahead of hold':>19s}")
    hold_staked = sum(b["nets"]["hold"] * b["stake"] for b in bets)
    for nm in NAMES:
        v = np.array([b["nets"][nm] for b in bets])
        st = float(sum(b["nets"][nm] * b["stake"] for b in bets))
        so = [b for b in bets if b["sold"].get(nm)]
        pd_, ph = defaultdict(float), defaultdict(float)
        for b in bets:
            pd_[b["day"]] += b["nets"][nm] * b["stake"]
            ph[b["day"]] += b["nets"]["hold"] * b["stake"]
        ahead = "" if nm == "hold" else f"{sum(pd_[d] > ph[d] + 1e-9 for d in pd_)} of {len(pd_)}"
        print(f"  {nm:20s} {len(so):5d} {sum(b['won'] for b in so):16d} {int((v <= -0.90).sum()):13d} {100 * v.mean():+12.2f} {st:+11.2f} {st - hold_staked:+13.2f} {ahead:>19s}")
    print()


print("window: live bets 2026-11-09 to 11-16 from their own buys; the loss side alone, against holding. Versions fixed before the run.\n")
five = {"commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial"}
table("THE FIVE LIVE COMMODITY LANES", *replay(f"{R.ROOT}/commodity_bets.csv", five))
table("commodity_multiscale (retired; same markets)", *replay(f"{R.ROOT}/commodity_bets.csv", {"commodity_multiscale"}))
table("ALL COMMODITY BETS", *replay(f"{R.ROOT}/commodity_bets.csv"))
table("crypto_multiscale", *replay(f"{R.ROOT}/lane_bets.csv"))
