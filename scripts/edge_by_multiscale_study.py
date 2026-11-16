"""Should the curve-edge floor move with the multiscale hour view? (2026-11-15 night, user: "this floor may end up needing to be swayed by a factor of our multiscale figure too")

One question, same bets (CLAUDE.md rule 4): every crypto_multiscale bet since the width change (2026-11-14 17:02 ET), live and the paper variants (the paper ones only widen the sample), with its result (its own exit or settlement, fee on every leg). Two readings at the buy: the "[curve edge x]" its decision note carries (the curve's chance minus the ask, the quantity buy_curve_edge_min floors), and the multiscale hour view's chance that the side pays (the geometric mean of the rates at 8, 16, 32, 64, 128 and 256 minutes from the Kalshi perp's own tape, the perp's distance to its window-open reference over rate x sqrt(time left): what the live hour view reads), called M. The table crosses the two: edge in three bands against M in three bands (under 0.5, 0.5 to 0.75, 0.75 and over): n, share that made money, net per dollar staked. Then, for the rule "floor = -0.03 - k x (M - 0.5)" (M above 0.5 loosens the floor, below tightens it), the bets it would skip at k = 0 (the plain floor of -0.03), 0.05, 0.10, 0.20 and 0.30 and what they made. (A first draft had the sign reversed and tightened the floor as M rose.) Not split by curve shape. A first look, thresholds chosen after seeing the table said to be so.

Run on the host: python scripts/edge_by_multiscale_study.py
"""
import bisect
import datetime
import math
import re
from collections import defaultdict
from statistics import NormalDist

from kalshi_moonshot import config, curverisk, db
from kalshi_moonshot.strategy import fee_per_contract
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
SCALES = (8, 16, 32, 64, 128, 256)
N = NormalDist()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
ticks = {}


def series_ticks(ser):
    if ser not in ticks:
        kp = perp_index(ser)
        rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts>? ORDER BY FLOOR(ts / 60), ts", (kp, SINCE - 17000)).fetchall() if kp else []
        ticks[ser] = ([float(r[0]) for r in rows], [float(r[1]) for r in rows])
    return ticks[ser]


bets = []
for r in st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome, close_time, reason FROM decisions WHERE lane LIKE 'crypto_multiscale%' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    m = re.search(r"\[curve edge ([+-]?\d+\.\d+)\]", str(r["reason"] or ""))
    ser = re.match(r"(KX[A-Z]+15M)-", r["ticker"])
    if n <= 0 or not m or not ser:
        continue
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    ts, vs = series_ticks(ser.group(1))
    if not ts:
        continue
    t = float(r["ts"]); close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp(); left = close - t
    i = bisect.bisect_left(ts, close - 900.0)
    j = bisect.bisect_right(ts, t) - 1
    if left <= 0 or i >= len(ts) or ts[i] - (close - 900.0) > 90.0 or j < 0 or t - ts[j] > 150.0:
        continue
    rate = curverisk.multiscale_rate(ts[:j + 1], vs[:j + 1], t, SCALES)
    if not rate or rate <= 0:
        continue
    sign = 1.0 if r["side"] == "yes" else -1.0
    big_m = N.cdf(sign * math.log(vs[j] / vs[i]) / (rate * math.sqrt(left)))
    bets.append((float(m.group(1)), big_m, v, cost))
print(f"bets with a curve edge, a perp hour and a result: {len(bets)}; net ${sum(b[2] for b in bets):+.2f} on ${sum(b[3] for b in bets):.0f}")
eb = (("edge < -0.03", -9, -0.03), ("edge -0.03 to 0", -0.03, 0.0), ("edge 0 and over", 0.0, 9))
mb = (("M < 0.5", -1, 0.5), ("M 0.5-0.75", 0.5, 0.75), ("M >= 0.75", 0.75, 2))
print("\nedge band x multiscale chance M: n | won | net per $ staked ($ net)")
for en, elo, ehi in eb:
    cells = []
    for mn, mlo, mhi in mb:
        g = [b for b in bets if elo <= b[0] < ehi and mlo <= b[1] < mhi]
        cells.append(f"{mn}: n {len(g):3d} won {sum(1 for b in g if b[2] > 0) / max(len(g), 1):4.0%} {sum(b[2] for b in g) / max(sum(b[3] for b in g), 1) * 100:+6.1f}% (${sum(b[2] for b in g):+7.2f})")
    print(f"  {en:16s} | " + " | ".join(cells))
print("\nthe rule floor = -0.03 - k x (M - 0.5): bets skipped (their edge under the moving floor), what they made, and what is left")
for k in (0.0, 0.05, 0.10, 0.20, 0.30):
    sk = [b for b in bets if b[0] < -0.03 - k * (b[1] - 0.5)]
    kp = [b for b in bets if b[0] >= -0.03 - k * (b[1] - 0.5)]
    print(f"  k = {k:4.2f}: skips {len(sk):3d} ({sum(1 for b in sk if b[2] > 0)} winners, {sum(1 for b in sk if b[2] <= 0)} losers) net ${sum(b[2] for b in sk):+8.2f}; the rest {len(kp)} bets net ${sum(b[2] for b in kp):+8.2f} ({sum(b[2] for b in kp) / max(sum(b[3] for b in kp), 1) * 100:+.1f}%)")
print("DONE")
