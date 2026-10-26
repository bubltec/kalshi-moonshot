"""CAN THE LADDER BE RECALIBRATED INTO AN EDGE? Fit a Platt map (and a pure vol rescale) on earlier ratings, score both on later ones.

The ladder's raw chances are compressed toward 50% (prob_above with COMMODITY_VOL_MULT=1.25 on top of realized
vol): on the settled record it says 0.85 about sides that win ~60-80% and 0.55 about sides that win ~66%, so
its Brier score loses to the ask it trades against on every lane that uses it. Two recalibrations, both fitted
only on the earlier part of the record and judged on the later part, each market counted once per lane at its
first rating (the same counting as report.py):

  platt    outcome ~ a + b * logit(p): b < 1 un-squashes the probabilities, a shifts them.
  rescale  outcome ~ logit(p) / k with the intercept refit: the one-parameter version, read as "the vol
           multiplier should have been 1.25 * k" (logit(p) = z / sigma, so dividing the logit by k is
           multiplying the sigma the pricer used by k).

For each: Brier of raw, recalibrated and the ask on the held-out part; and the bet test — buy held-out
ratings whose (recalibrated) chance beats the ask by at least the fee plus 1/3/5 points, return per dollar
after the entry fee, standard error clustered by 15-minute slot where the market is one.

Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/ladder_calibration_study.py
"""
import collections
import math
import statistics

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
main = Store(s.db_path, s.exclude_versions)

LANES = ("commodity", "crypto_favorite", "fx_favorite", "crypto_socket", "fx_favorite_ctrl", "mid_trail", "fx_trail30", "late_ticket")


def logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z)) if z > -30 else 0.0


def fit_logistic(xs, ys):
    """Newton's method for outcome ~ sigmoid(a + b*x). Returns (a, b)."""
    a = b = 0.0
    b = 1.0
    for _ in range(60):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys, strict=True):
            p = sigmoid(a + b * x)
            w = max(p * (1 - p), 1e-9)
            ga += y - p
            gb += (y - p) * x
            haa += w
            hab += w * x
            hbb += w * x * x
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da, db_ = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a + da, b + db_
        if abs(da) + abs(db_) < 1e-10:
            break
    return a, b


rows = []
seen = set()
for r in main.db.execute(
    "SELECT ts, ticker, lane, side, ask, model_p, outcome, close_time FROM decisions "
    "WHERE model_p IS NOT NULL AND outcome IN ('yes','no') AND ask > 0 AND ask < 1 ORDER BY ts"
).fetchall():
    key = (r["lane"], r["ticker"])
    if key in seen:
        continue
    seen.add(key)
    rows.append(r)

by_lane: dict[str, list] = collections.defaultdict(list)
for r in rows:
    if r["lane"] in LANES:
        by_lane[r["lane"]].append(r)
pooled = [r for lane in LANES for r in by_lane.get(lane, [])]
pooled.sort(key=lambda r: r["ts"])


def brier(preds, ys):
    return sum((p - y) ** 2 for p, y in zip(preds, ys, strict=True)) / len(ys) if ys else float("nan")


def evaluate(label, group):
    group = sorted(group, key=lambda r: r["ts"])
    cut = int(len(group) * 0.6)
    fit_rows, test = group[:cut], group[cut:]
    if len(fit_rows) < 100 or len(test) < 50:
        print(f"  {label:18} only {len(group)} ratings")
        return
    ys_fit = [1.0 if r["outcome"] == (r["side"] or "yes") else 0.0 for r in fit_rows]
    a, b = fit_logistic([logit(float(r["model_p"])) for r in fit_rows], ys_fit)
    ys = [1.0 if r["outcome"] == (r["side"] or "yes") else 0.0 for r in test]
    raw = [float(r["model_p"]) for r in test]
    asks = [float(r["ask"]) for r in test]
    platt = [sigmoid(a + b * logit(p)) for p in raw]
    # the one-parameter rescale: intercept refit by matching the held-out... no, by the fit part's base rate at k
    best = None
    for k10 in range(4, 31):
        k = k10 / 10.0
        cand = [sigmoid(logit(float(r["model_p"])) / k) for r in fit_rows]
        shift = statistics.mean(ys_fit) - statistics.mean(cand)
        score = brier([min(1 - 1e-4, max(1e-4, c + shift)) for c in cand], ys_fit)
        if best is None or score < best[0]:
            best = (score, k, shift)
    _, k, shift = best
    resc = [min(1 - 1e-4, max(1e-4, sigmoid(logit(p) / k) + shift)) for p in raw]
    print(f"  {label:18} fit on {len(fit_rows)}, test on {len(test)}:  Brier raw {brier(raw, ys):.4f}  platt {brier(platt, ys):.4f}  rescale {brier(resc, ys):.4f}  ask {brier(asks, ys):.4f}")
    print(f"  {'':18} platt a={a:+.3f} b={b:.3f} (b<1 pulls the raw chances toward 50%, b>1 pushes them out); rescale k={k:.1f} means the vol width should be x{k:.1f} of what the pricer used")
    for name, preds in (("raw", raw), ("platt", platt), ("rescale", resc)):
        for bar in (0.01, 0.03, 0.05):
            picks = [(preds[i], asks[i], ys[i], test[i]) for i in range(len(test)) if preds[i] - asks[i] - fee(asks[i]) >= bar]
            if len(picks) < 20:
                continue
            per_slot = collections.defaultdict(list)
            for _p, a_, y_, r in picks:
                c = a_ + fee(a_)
                slot = r["close_time"] or r["ticker"]
                per_slot[slot].append((y_ - c) / c)
            means = [sum(v) / len(v) for v in per_slot.values()]
            se = (100 * math.sqrt(sum((x - statistics.mean(means)) ** 2 for x in means) / (len(means) - 1) / len(means)) if len(means) > 2 else float("nan"))
            rets = [(y_ - (a_ + fee(a_))) / (a_ + fee(a_)) for _, a_, y_, _ in picks]
            print(f"  {'':18} {name:8} edge >= {100 * bar:.0f}c+fee: {len(picks):4} bets in {len(per_slot):4} slots, return {100 * statistics.mean(rets):+6.1f}% +-{se:.0f} (by slot)")


print(f"{len(pooled)} first-ratings from the ladder lanes, {len(rows)} across all lanes")
evaluate("all ladder lanes", pooled)
for lane in LANES:
    evaluate(lane, by_lane.get(lane, []))
