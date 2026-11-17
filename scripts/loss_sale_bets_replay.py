"""Loss-sale versions replayed on the live lane's own bets.

Question (user, 2026-11-17, after the loss sale sold KXETH15M-26NOV171545 at 0.20 and the market won: "why don't we fix it rather than leave us vulnerable?"): on the bets crypto_multiscale really made, which loss sale removes total losses at the least cost, and how many winners does each sell?
One question on the same bets: every live bet of the lane from 2026-11-09 to 11-16 with quotes on the archive, from its own buy time and price. Each is held to settlement, or sold by one loss-sale version at the side's bid (the fee on the sale; the buy as filled). The lane's profit take and its other exits are left out on purpose: this is the loss side alone, against holding.
$1 a bet (every bet counted the same) and as really staked. A total loss: the bet ends at 90% or more of its stake lost.

Versions (the first is what was live on 2026-11-17; the others were written down before this run):
  live today      the line is the nearer of two wobbles under the buy's bid and half the price paid; the wait is the scaled one (curverisk.stop_wait, recovery time 60 s)
  quarter wait    the same line; the wait is a quarter of the time left, whatever the depth
  sliding line    nothing is sold while more than half the market is left; from there the line rises in a straight line from 0 to half the price paid at the close; quarter wait
  last 3 minutes  the half-price line, only in the last fifth of the market; quarter wait
  0.12 line       sold when the bid is at or under 0.12; quarter wait (the lane's risk exit as it stands)
Reads data/tape_archive/lane_bets.csv (ticker,ts,side,price,stake from the host's decisions), windows.csv and the archive's quotes. Nothing on the host.

    .venv/bin/python scripts/loss_sale_bets_replay.py
"""
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import risk_attribute_replay as R  # noqa: E402

from kalshi_moonshot import curverisk  # noqa: E402
from kalshi_moonshot.strategy import fee_per_contract as fee  # noqa: E402

ET = ZoneInfo("America/New_York")
quotes = R._quotes_15m()
wins = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open(f"{R.ROOT}/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}


def wobble(qt, qb, qa, ts):
    g = np.arange(qt[0], ts + 5.0, 5.0)
    if len(g) < 8:
        return None
    m = ((qb + qa) / 2.0)[np.clip(np.searchsorted(qt, g, side="right") - 1, 0, len(qt) - 1)]
    v = float(np.sqrt(np.mean(np.diff(m) ** 2)) * np.sqrt(12.0))
    return v if v > 0 else None


def run(times, sb, close, line_at, wait_at, ok_at=None):
    """The first moment the bid has stayed at or under its line for the wait (and, with `ok_at`, the spot agreed the whole time); returns the index or None."""
    since = None
    for i in range(len(times)):
        left = close - times[i]
        if left <= 0:
            break
        ln = line_at(left)
        if ln is None or sb[i] > ln + 1e-9 or sb[i] <= 0 or (ok_at is not None and not ok_at(times[i])):
            since = None
            continue
        if since is None:
            since = times[i]
        if times[i] - since >= wait_at(sb[i], ln, left) - 1e-9:
            return i
    return None


NAMES = ["hold", "live today", "spot behind", "spot behind 0.5", "spot behind 1.0", "quarter wait", "sliding line", "last 3 minutes", "0.12 line"]
# 2026-11-17, second run (user: "how can we tweak so it doesn't sell just because of a couple dips?"; "does it care about the price at all? or the amount it dipped?"): the live version reads the price paid and the depth of the dip and nothing else. "spot behind": the same line and wait, but the bid only counts as under the line while the index is on the wrong side of the target for the side (the cushion, in the index's typical moves for the time left, under 0; under -0.5; under -1.0). A dip in the contract's price with the index still on the bet's side is not sold.
CUSH = {}
for r_ in list(csv.reader(open(R.OUT)))[1:]:
    CUSH.setdefault(r_[0], []).append((int(r_[1]), float(r_[7])))
import bisect  # noqa: E402


def cushion_at(tk, side, t):
    rows_ = CUSH.get(tk)
    if not rows_:
        return None
    i = bisect.bisect_right(rows_, (t, 9e9)) - 1
    if i < 0 or t - rows_[i][0] > 45:
        return None
    return rows_[i][1] if side == "yes" else -rows_[i][1]

bets, skipped = [], 0
for r in csv.reader(open(f"{R.ROOT}/lane_bets.csv")):
    if len(r) != 5:
        continue
    tk, ts, side, paid, stake = r[0], float(r[1]), r[2], float(r[3]), float(r[4])
    if tk not in quotes or tk not in wins:
        skipped += 1
        continue
    close, result = wins[tk]
    qt, qb, qa = quotes[tk]
    a = int(np.searchsorted(qt, ts, side="right"))
    if a < 1 or a >= len(qt):
        skipped += 1
        continue
    sb = (qb[a:] if side == "yes" else 1.0 - qa[a:]).tolist()
    times = qt[a:].tolist()
    bid0 = float(qb[a - 1] if side == "yes" else 1.0 - qa[a - 1])
    wob = wobble(qt, qb, qa, ts)
    half = 0.5 * paid
    near = max(bid0 - 2.0 * wob, half) if wob else half
    won = result == side
    cost = paid + fee(paid)
    held = ((1.0 if won else 0.0) - cost) / cost
    quarter = lambda b, ln, left: 0.25 * left  # noqa: E731
    versions = {
        "live today": (lambda left: near, lambda b, ln, left: curverisk.stop_wait(b, ln, left, 60.0, 0.25)),
        "quarter wait": (lambda left: near, quarter),
        "sliding line": (lambda left: half * (1.0 - min(1.0, left / 450.0)) if left < 450.0 else None, quarter),
        "last 3 minutes": (lambda left: half if left <= 180.0 else None, quarter),
        "0.12 line": (lambda left: 0.12, quarter),
    }
    live_wait = lambda b, ln, left: curverisk.stop_wait(b, ln, left, 60.0, 0.25)  # noqa: E731

    def behind(depth):
        return lambda t_, tk=tk, side=side: (lambda c: c is not None and c < -depth)(cushion_at(tk, side, t_))

    versions |= {"spot behind": (lambda left: near, live_wait, behind(0.0)), "spot behind 0.5": (lambda left: near, live_wait, behind(0.5)), "spot behind 1.0": (lambda left: near, live_wait, behind(1.0))}
    nets, sold = {"hold": held}, {}
    for name, spec_ in versions.items():
        line_at, wait_at = spec_[0], spec_[1]
        i = run(times, sb, close, line_at, wait_at, spec_[2] if len(spec_) > 2 else None)
        if i is None:
            nets[name] = held
        else:
            s = min(max(sb[i], 0.01), 0.99)
            nets[name] = (s - fee(s) - cost) / cost
            sold[name] = True
    bets.append(dict(nets=nets, sold=sold, won=won, stake=stake, paid=paid, age=ts - (close - 900.0), day=datetime.datetime.fromtimestamp(ts, ET).strftime("%m-%d")))

days = sorted({b["day"] for b in bets})
print(f"window: crypto_multiscale's live bets, {days[0]} to {days[-1]} ET, with quotes on the archive: {len(bets)} bets ({skipped} without quotes or a result), ${sum(b['stake'] for b in bets):.0f} staked; {sum(b['won'] for b in bets)} won at settlement")
print("each bet from its own buy: held, or sold by one loss sale. The profit take and the lane's other exits are not in it. Versions were fixed before the run.\n")


def table(title, sel):
    print(title)
    print(f"  {'version':16s} {'sold':>5s} {'of them winners':>16s} {'total losses':>13s} {'$1 a bet':>10s} {'cents per $':>12s} {'as staked':>11s} {'days ahead of hold':>19s}")
    for nm in NAMES:
        v = np.array([b["nets"][nm] for b in sel])
        st = np.array([b["nets"][nm] * b["stake"] for b in sel])
        so = [b for b in sel if b["sold"].get(nm)]
        tl = int((v <= -0.90).sum())
        pd_, ph = defaultdict(float), defaultdict(float)
        for b in sel:
            pd_[b["day"]] += b["nets"][nm]
            ph[b["day"]] += b["nets"]["hold"]
        ahead = "" if nm == "hold" else f"{sum(pd_[d] > ph[d] + 1e-9 for d in pd_)} of {len(pd_)}"
        print(f"  {nm:16s} {len(so):5d} {sum(b['won'] for b in so):16d} {tl:13d} {v.sum():+10.2f} {100 * v.mean():+12.2f} {st.sum():+11.2f} {ahead:>19s}")
    print()


table("ALL BETS", bets)
table("bought in the first 6 minutes", [b for b in bets if b["age"] < 360])
table("bought in minutes 6 to 10", [b for b in bets if 360 <= b["age"] < 600])
table("bought in the last 5 minutes", [b for b in bets if b["age"] >= 600])
table("paid under 0.60", [b for b in bets if b["paid"] < 0.60])
table("paid 0.60 to 0.75", [b for b in bets if 0.60 <= b["paid"] < 0.75])
table("paid 0.75 and over", [b for b in bets if b["paid"] >= 0.75])
table("the last three days (larger stakes)", [b for b in bets if b["day"] in days[-3:]])
