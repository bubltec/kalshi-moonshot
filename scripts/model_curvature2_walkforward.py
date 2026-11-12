"""Do the three ideas from the 1989 curvature-dependent turbulence paper lift the curve model? (2026-11-12, user: "i do like the 3 points above, lets integrate that into our logic")
Question, one: the same walk-forward as scripts/model_alone_baseline.py (settled 15-minute markets since Monday 2026-11-09, fitted on the earlier days and scored on the next, held to settlement with the entry fee, the model's own bar of the top 15% and above zero, the first moment per market), with the model reading `curvature2` (curvemodel.expand_curvature2: the curvature split by sign at each window, and the saturating ratio sat_13m) in place of `curvature`. Everything else is identical, so a difference is the new readings.
Lines, each for the old and the new readings: A (price band 0.70-0.90 and minutes 7-15 as the live lane has them) and B (gates off: ask 0.05-0.95, minutes 1-14). The edge floor and flat-trend gate are not replayed; sales are left out. Dollars at $5 a bet.
The three ideas: (1) the ratio form (sat_13m), (2) convex and concave bends as separate readings (bendup and benddown at each window), (3) the zonal point, checked by the split by curve shape: the picks by terciles of the readings that carry the new ideas, for the new model, next to the old model's picks on the same readings.
No halves (the user dropped them, 2026-11-12). Nothing is chosen after the results.
    scripts/cloud_job.sh scripts/model_curvature2_walkforward.py
"""
import dataclasses, datetime as dt, time
from kalshi_moonshot import config, curvemodel, lanes
from kalshi_moonshot.curvemodel import KINDS, cut_for, expand_curvature2, fit, predict

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
    return [(r, m) for r, m in zip(rows, meta) if m[0] > MON + 60]


gated = rows_for(False)
open_ = rows_for(True)
print(f"rows since Monday: gated-setting {len(gated)}, gates-off {len(open_)}; the tape ends {hm(max(m[0] for _, m in open_))} ET")
day = lambda close: int((close - MON) // 86400)  # noqa: E731
days = sorted({day(m[0]) for _, m in open_})
print("days (0 = Mon 11-09):", days)


def view(rows, kind):
    """Rows as (readings of `kind`, ask, net), keeping the meta."""
    out = []
    for (read, ask, net), m in rows:
        r9 = list(read[:9])
        out.append(((expand_curvature2(r9) if kind == "curvature2" else r9, ask, net), m))
    return out


def picks(model, kind, test, allow=None):
    cut = cut_for(model, TOP)
    n = len(KINDS[kind])
    best = {}
    for (read, ask, net), m in test:
        if allow and not allow(ask, m[1]):
            continue
        p = predict(model, read[:n], ask)
        if p <= 0 or p < cut:
            continue
        cur = best.get(m[2])
        if cur is None or (m[1], -p) < (cur[5], -cur[7]):
            best[m[2]] = (m[0], m[2], m[3], ask, net, m[1], read[0], p, list(read))
    return list(best.values())


def line(label, ps):
    if not ps:
        return f"  {label:40s} n=0"
    d = [STAKE * x[4] for x in ps]
    stake = STAKE * len(ps)
    return f"  {label:40s} n={len(ps):4d} | {sum(d):+8.2f} on ${stake:7.2f} ({sum(d) / stake:+6.1%}) | wins {sum(1 for x in ps if x[4] > 0)}/{len(ps)}"


R = {}
for kind in ("curvature", "curvature2"):
    g_, o_ = view(gated, kind), view(open_, kind)
    A, B = [], []
    for td in days[1:]:
        mg = fit([r for r, m in g_ if day(m[0]) < td], kind=kind)
        mo = fit([r for r, m in o_ if day(m[0]) < td], kind=kind)
        if mg:
            A += picks(mg, kind, [x for x in g_ if day(x[1][0]) == td])
        if mo:
            B += picks(mo, kind, [x for x in o_ if day(x[1][0]) == td])
    R[kind] = (A, B)
for kind in ("curvature", "curvature2"):
    A, B = R[kind]
    print(f"\nreadings: {kind}")
    print(line("A  live band and minutes", A))
    print(line("B  gates off", B))
    print(line("B  ask 0.90-1.00", [x for x in B if x[3] >= 0.90]))
    print(line("B  ask 0.70-0.90", [x for x in B if 0.70 <= x[3] < 0.90]))
    print(line("B  ask under 0.70", [x for x in B if x[3] < 0.70]))
    print(line("B  minute 1-7", [x for x in B if x[5] < 420]))
    print(line("B  minute 7-14", [x for x in B if x[5] >= 420]))
oldB, newB = {x[1] for x in R["curvature"][1]}, {x[1] for x in R["curvature2"][1]}
print(f"\nB picks: old {len(oldB)} markets, new {len(newB)}, both {len(oldB & newB)}")
print(line("B old only", [x for x in R["curvature"][1] if x[1] not in newB]))
print(line("B new only", [x for x in R["curvature2"][1] if x[1] not in oldB]))
print(line("B both, old model's side and price", [x for x in R["curvature"][1] if x[1] in newB]))
print(line("B both, new model's side and price", [x for x in R["curvature2"][1] if x[1] in oldB]))
print("\nthe picks by terciles of the readings that carry the ideas (B; cuts from each model's own picks)")
names = KINDS["curvature2"]
for kind in ("curvature", "curvature2"):
    B = R[kind][1]
    for nm in ("level_13m", "slope_13m", "bendup_13m", "benddown_13m", "sat_13m"):
        j9 = {"level_13m": 3, "slope_13m": 4}.get(nm)
        val = (lambda x, j9=j9: x[8][j9]) if j9 is not None and kind == "curvature" else None
        if kind == "curvature2" or j9 is not None:
            idx = names.index(nm) if kind == "curvature2" else j9
        else:
            continue
        v = sorted(x[8][idx] for x in B)
        if len(v) < 30:
            continue
        c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
        print(f" {kind}: {nm} (cuts {c1:+.2f}, {c2:+.2f})")
        for lab, f in (("low", lambda x: x[8][idx] < c1), ("mid", lambda x: c1 <= x[8][idx] < c2), ("high", lambda x: x[8][idx] >= c2)):
            print(line("   " + lab, [x for x in B if f(x)]))
print("DONE")
