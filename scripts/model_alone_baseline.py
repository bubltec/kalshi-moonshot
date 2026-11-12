"""What does the curve model do with the gates taken away? (2026-11-12, user: "the goal is for us to remove the gates and let the curve model do the work ... we need it to be smart enough to bet on its own"; "yes" to the gates-off baseline on the tape)
Question, one: on settled 15-minute markets since Monday 2026-11-09 (the user's cutoff), walk-forward (the model is fitted on the earlier days only and scored on the next day), held to settlement with the entry fee, does the curvature model's own bar (top 15% of the training moments and above zero, the live setting) pay when it is the only thing deciding?
Three lines, same test days, same bets-per-market rule (the first moment of a market that meets the bar, either side; 30-second moments from minute 1 to 14):
  A  as the live lane runs it: the model fitted on, and the bets limited to, ask 0.70-0.90 and minutes 7-15 (the lane's price band and entry_elapsed; its spread cap stays in every line because it is a fill fact, not a view)
  B  gates off: the model fitted on every moment with ask 0.05-0.95 and minutes 1-14, and the bets taken wherever it clears its bar
  C  B's model with A's price band and minutes applied to its picks (isolates what the two gates add or remove)
The edge floor (buy_curve_edge_min) and the flat-trend gate are not in the model's rows; they are applied by the lane after the model and are not replayed here. So A is the model plus band plus minutes, not the whole lane. Dollars are at $5 a bet; every sale rule is left out (held to settlement), so this is the buying side only.
Reported: n, markets, net dollars, per dollar staked, wins, halves in time, and for B the picks by price, by minute and by the spot's distance to the strike at the buy (terciles of the 5-minute level reading, a curve reading, not an asset). Nothing is chosen after the results.
The tape the job restores ends where the nightly backup ends (said in the first line).
    scripts/cloud_job.sh scripts/model_alone_baseline.py
"""
import dataclasses, datetime as dt, time
from kalshi_moonshot import config, curvemodel, lanes
from kalshi_moonshot.curvemodel import KINDS, cut_for, fit, predict

s = config.reload()
ET = dt.timezone(dt.timedelta(hours=-5))
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
curvemodel.TRAIN_DAYS = (time.time() - MON) / 86400.0 + 0.1
TOP, STAKE = 0.15, 5.0
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
orig = lanes.lane_specs


def wide(st):
    return {k: (dataclasses.replace(v, price=(0.05, 0.95), entry_elapsed=(1.0, 14.0), hours=(0.0, 0.26)) if v.model_buy else v) for k, v in orig(st).items()}


def rows_for(patch):
    lanes.lane_specs = wide if patch else orig
    meta = []
    rows = curvemodel.build_rows(s, kind="curvature", duty=1.0, meta=meta)
    lanes.lane_specs = orig
    keep = [(r, m) for r, m in zip(rows, meta) if m[0] > MON + 60]
    return keep


gated = rows_for(False)
open_ = rows_for(True)
print(f"rows since Monday: gated-setting {len(gated)}, gates-off {len(open_)}; the tape ends {hm(max(m[0] for _, m in open_))} ET")
day = lambda close: int((close - MON) // 86400)  # noqa: E731
days = sorted({day(m[0]) for _, m in open_})
print("days (0 = Mon 11-09):", days, "rows per day gates-off:", {d: sum(1 for _, m in open_ if day(m[0]) == d) for d in days})


def picks(model, test, bar_only_in=None):
    """First moment per market (either side) with predicted net >= the model's bar and > 0. test: [(row, meta)]. Returns [(close, ticker, side, ask, net, elapsed, level)]."""
    cut = cut_for(model, TOP)
    best = {}
    for (read, ask, net), m in test:
        if bar_only_in and not bar_only_in(ask, m[1]):
            continue
        p = predict(model, read[: len(KINDS["curvature"])], ask)
        if p <= 0 or p < cut:
            continue
        cur = best.get(m[2])
        if cur is None or (m[1], -p) < (cur[5], -cur[7]):
            best[m[2]] = (m[0], m[2], m[3], ask, net, m[1], read[0], p)
    return list(best.values())


def line(label, ps):
    if not ps:
        return f"  {label:36s} n=0"
    ps = sorted(ps, key=lambda x: x[0])
    d = [STAKE * x[4] for x in ps]
    h = len(ps) // 2
    stake = STAKE * len(ps)
    wins = sum(1 for x in ps if x[4] > 0)
    return f"  {label:36s} n={len(ps):4d} | {sum(d):+8.2f} on ${stake:7.2f} ({sum(d) / stake:+6.1%}) | wins {wins}/{len(ps)} | halves {sum(d[:h]):+.2f} / {sum(d[h:]):+.2f}"


A, B, C = [], [], []
for td in days[1:]:
    train_g = [r for r, m in gated if day(m[0]) < td]
    train_o = [r for r, m in open_ if day(m[0]) < td]
    mg = fit([(r[0], r[1], r[2]) for r in train_g], kind="curvature")
    mo = fit([(r[0], r[1], r[2]) for r in train_o], kind="curvature")
    print(f"\ntest day {td} ({dt.datetime.fromtimestamp(MON + td * 86400 + 3600, ET):%m-%d}): trained on {len(train_g)} gated rows / {len(train_o)} gates-off rows; fitted: {mg is not None} / {mo is not None}")
    if mg:
        A += picks(mg, [(r, m) for r, m in gated if day(m[0]) == td])
    if mo:
        T = [(r, m) for r, m in open_ if day(m[0]) == td]
        B += picks(mo, T)
        C += picks(mo, T, lambda ask, e: 0.70 <= ask <= 0.90 and e >= 420)
print()
print(line("A  live setting (band, minutes)", A))
print(line("B  gates off", B))
print(line("C  gates-off model, live band+minutes", C))
print("\nB by the price paid")
for lo, hi in ((0.0, 0.50), (0.50, 0.70), (0.70, 0.90), (0.90, 1.0)):
    print(line(f"ask {lo:.2f}-{hi:.2f}", [x for x in B if lo <= x[3] < hi or (hi == 1.0 and x[3] == 1.0)]))
print("B by minute of the window")
for lo, hi in ((60, 240), (240, 420), (420, 660), (660, 900)):
    print(line(f"minute {lo // 60}-{hi // 60}", [x for x in B if lo <= x[5] < hi]))
if B:
    lv = sorted(x[6] for x in B)
    c1, c2 = lv[len(lv) // 3], lv[2 * len(lv) // 3]
    print(f"B by the spot's distance to the strike on the bet's side (5-minute level reading; cuts {c1:+.2f}, {c2:+.2f} typical minutes)")
    print(line("closest to the strike", [x for x in B if x[6] < c1]))
    print(line("middle", [x for x in B if c1 <= x[6] < c2]))
    print(line("farthest on the bet's side", [x for x in B if x[6] >= c2]))
print("DONE")
