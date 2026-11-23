"""PriskP favorite-exit replay (2026-11-23; user: the staircase is built for scalping, PriskP buys favorites at 0.70-0.85; "our goal is avoiding loss", "prefers small wins over a potential loss"; no cap per window).
QUESTION (one, on the same bets): on every filled, settled PriskP bet (paper), does an exit built for a favorite beat what the exit really did and holding? Fee on every leg (0.07 p (1-p)).
RULES (stated before the run, not a sweep; the bid is the bet's own side bid from the recorded quotes; u = the bid's typical minute = rms one-second change of the side's mid over the 900 s before the buy x sqrt(60)):
  TAKE:  sell the first time the net after both fees is 0.5 x u or more above zero (lead from priskp_loss_exit_replay.py; 0.5 was chosen there after seeing 0.25 and 1.0).
  VALUE: sell the first time the bid after the sale fee is at least the hold value 1 - p_lose, p_lose = wavetop.p_lose(spot cushion, spot unit, seconds left) read from Kalshi's own settlement index ticks (the 60 s average's distance past the strike on the bet's side, wavetop.spot_read; no reading, no sale). It sells when the market pays what the spot says the bet is worth, at a gain or a loss. Margins 0.02 and 0.05 under the hold value are shown beside it, not chosen.
  BOTH:  whichever comes first.
A sale is at the recorded bid of that second; a fill is not modelled. One day of paper bets; a lead only; read-only; the index is the settlement index of the series (kalshiws.INDEX_FOR).
Run: DUMP=bets_and_quotes.json SPOT=spot.json .venv/bin/python scripts/priskp_favorite_exit_replay.py (both files made on the host by read-only queries)."""
import json
import math
import os

from kalshi_moonshot import wavetop

data, spot = json.load(open(os.environ["DUMP"])), json.load(open(os.environ["SPOT"]))


def fee(p):
    return 0.07 * p * (1.0 - p)


WAITS = (0.1, 0.25, 0.5)  # the value sale waits until its condition has held without a break for this share of the time left (2026-11-23, user: it was the first wave it hit, "it should have given more time"); three shares shown, none chosen yet; waitx = the same, sold only after the spot crossed
MARGINS = (-0.05, -0.02, 0.0, 0.02, 0.05)
HOLDS = (0.005, 0.01, 0.02, 0.05, 0.10)  # the hold step: no value sale while p_lose is at or under this bar (2026-11-23, user: hold the clearly safe ones for the small gain); a bar is a chance, shown at five levels, none chosen yet
res, skipped = [], 0
for b in data["bets"]:
    t0, close, n = float(b["ts"]), float(b["close_ts"]), float(b["contracts"])
    basis, side = float(b["stake_usd"]) / n, b["side"]
    q, sp = data["quotes"][b["ticker"]], spot.get(b["ticker"])
    if sp and os.environ.get("PERP"):  # SPOT holds the coin's perp ticks: the target is the perp's own value at the window's open (a tick within 60 s of it)
        opened = [x for x in sp["ticks"] if close - 900 <= x[0] <= close - 840]
        sp = dict(sp, strike=opened[0][1] if opened else None)
    if not sp or not sp["ticks"] or not sp["strike"]:
        skipped += 1
        continue
    sb = [(x[0], x[1] if side == "yes" else 1.0 - x[2], x[1], x[2]) for x in q]
    grid, j = [], 0
    for sec in range(int(t0 - 900), int(close - 1)):
        while j + 1 < len(sb) and sb[j + 1][0] <= sec:
            j += 1
        grid.append((sec, sb[j][1]))
    pre = [g[1] for g in grid if g[0] < t0]
    dd = [pre[i + 1] - pre[i] for i in range(len(pre) - 1)]
    if len(dd) < 120:
        skipped += 1
        continue
    u = math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0)
    T = [float(t[0]) for t in sp["ticks"]]
    V = [float(t[1]) for t in sp["ticks"]]
    won = b["result"] == side
    held = (1.0 if won else 0.0) - basis - fee(basis)
    sold = b["exit_price"] is not None and b["exit_ts"] is not None and float(b["exit_ts"]) < close - 1
    actual = ((float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)) - basis - fee(basis)
    cache = {}

    cush = {}

    def hold_value(sec):
        if sec not in cache:
            k = sum(1 for x in T if x <= sec)  # ticks up to now only
            r = wavetop.spot_read(T[:k], V[:k], float(sec), float(sp["strike"]), side) if k else None
            cache[sec] = None if r is None else 1.0 - wavetop.p_lose(r[0], r[1], close - sec)
            cush[sec] = None if r is None else r[0]
        return cache[sec]

    def lose_now(sec):
        hv = hold_value(sec)
        return None if hv is None else 1.0 - hv

    tick_at = {int(x[0]): float(x[1]) for x in sp["ticks"]}

    def inst_cushion(sec):  # the latest perp tick at or before this second, against the target, for the bet's side (negative = crossed)
        for back in range(0, 8):
            v = tick_at.get(sec - back)
            if v is not None:
                return (v - float(sp["strike"])) if side == "yes" else (float(sp["strike"]) - v)
        return None

    def avg_cushion(sec, n):  # the perp's average of the last n seconds against the target, for the bet's side
        vals = [tick_at[k] for k in range(sec - n + 1, sec + 1) if k in tick_at]
        if len(vals) < max(2, n // 3):
            return None
        a = sum(vals) / len(vals)
        return (a - float(sp["strike"])) if side == "yes" else (float(sp["strike"]) - a)

    def projection(sec, w=45):
        """(seconds until the perp's cushion reaches zero at its present rate, the line's R squared) from a straight line through the last w seconds of the bet's side cushion; None when the cushion is not closing or there are too few ticks."""
        pts = [(k, (tick_at[k] - float(sp["strike"])) if side == "yes" else (float(sp["strike"]) - tick_at[k])) for k in range(sec - w + 1, sec + 1) if k in tick_at]
        if len(pts) < max(8, w // 3):
            return None
        mx = sum(k for k, _ in pts) / len(pts)
        my = sum(c for _, c in pts) / len(pts)
        sxx = sum((k - mx) ** 2 for k, _ in pts)
        sxy = sum((k - mx) * (c - my) for k, c in pts)
        syy = sum((c - my) ** 2 for _, c in pts)
        if sxx <= 0 or syy <= 0:
            return None
        b = sxy / sxx
        if b >= 0:
            return None  # the cushion is not closing
        now_fit = my + b * (sec - mx)
        r2 = (sxy * sxy) / (sxx * syy)
        return (max(now_fit, 0.0) / -b, r2)

    def perp_unit(sec):  # the perp's typical minute: rms one-second change over the 10 minutes before, x sqrt(60)
        ks = [k for k in range(sec - 600, sec) if k in tick_at and k + 1 in tick_at]
        if len(ks) < 120:
            return None
        return math.sqrt(sum((tick_at[k + 1] - tick_at[k]) ** 2 for k in ks) / len(ks)) * math.sqrt(60.0)

    def run_proj_units(speed_min, near_max):
        """The angle and the rate in the spot's own units (one stated rule, 2026-11-23): sell at the bid when the cushion is closing in a straight line (R squared 0.8 over 45 s), at least `speed_min` typical minutes a minute, from a cushion of at most `near_max` typical minutes, and at that rate reaches zero before the close."""
        for sec, bid in grid:
            if sec <= t0 or sec % 5:
                continue
            pr = projection(sec)
            if pr is None or pr[1] < 0.8:
                continue
            u = perp_unit(sec)
            if not u:
                continue
            ttc = pr[0]
            speed = None
            pts = [(k, (tick_at[k] - float(sp["strike"])) if side == "yes" else (float(sp["strike"]) - tick_at[k])) for k in range(sec - 44, sec + 1) if k in tick_at]
            if len(pts) < 15:
                continue
            mx = sum(k for k, _ in pts) / len(pts)
            my = sum(c for _, c in pts) / len(pts)
            b = sum((k - mx) * (c - my) for k, c in pts) / sum((k - mx) ** 2 for k, _ in pts)
            speed = -b * 60.0 / u
            cushion_u = max(my + b * (sec - mx), 0.0) / u
            if speed >= speed_min and cushion_u <= near_max and ttc <= close - sec:
                return bid - fee(bid) - basis - fee(basis)
        return held

    def run_proj(share, r2_min):
        """The angle and the rate (user, on the BTC 16:30 bet: "we should have exited sooner"): sell at the bid when the cushion is closing in a straight enough line (R squared at least r2_min over the last 45 s) and, at that rate, reaches the target in under `share` of the time left."""
        for sec, bid in grid:
            if sec <= t0 or sec % 5:
                continue
            pr = projection(sec)
            if pr is not None and pr[1] >= r2_min and pr[0] <= share * (close - sec):
                return bid - fee(bid) - basis - fee(basis)
        return held

    def run_cross_n(n):
        """Sell at the bid the first time the perp's n-second average is on the wrong side of the target; no value condition."""
        for sec, bid in grid:
            if sec <= t0 or sec % 5:
                continue
            c = avg_cushion(sec, n)
            if c is not None and c <= 0:
                return bid - fee(bid) - basis - fee(basis)
        return held

    def run_cross(avg60):
        """Sell at the bid the first time the spot has crossed to the wrong side of the target (cushion zero or under): the 60 s average the value sale reads (avg60) or the latest tick, with no value condition."""
        for sec, bid in grid:
            if sec <= t0 or sec % 5:
                continue
            if avg60:
                hold_value(sec)
                c = cush.get(sec)
            else:
                c = inst_cushion(sec)
            if c is not None and c <= 0:
                return bid - fee(bid) - basis - fee(basis)
        return held

    def run(take, value_margin, hold_p=None, crossed=False, wait_share=0.0):
        since = None  # when the value sale's condition began to hold without a break (the wait is a share of the time left, never fixed seconds)
        for sec, bid in grid:
            if sec <= t0:
                continue
            net = bid - fee(bid) - basis - fee(basis)
            if take and net >= 0.5 * u:
                return net
            if value_margin is not None and sec % 5 == 0:
                hv = hold_value(sec)
                pl = lose_now(sec)
                if crossed and (cush.get(sec) is None or cush[sec] > 0):
                    continue  # the spot has not crossed to the wrong side of the target: no sale
                if hold_p is not None and pl is not None and pl <= hold_p:
                    continue  # the hold step: a bet clearly safe by the spot is not asked the value sale
                if hv is not None and bid - fee(bid) >= hv - value_margin:
                    since = sec if since is None else since
                    if sec - since >= wait_share * (close - sec):
                        return net
                else:
                    since = None
        return held

    out = {"actual": actual, "hold": held, "n": n, "won": won, "take": run(True, None), "both": run(True, 0.0)}
    for m in MARGINS:
        out[f"value{m}"] = run(False, m)
    out["crossed0.0"] = run(False, 0.0, None, True)
    out["crossed0.02"] = run(False, -0.02, None, True)  # a premium of 0.02 over the hold value
    out["projU"] = run_proj_units(1.0, 1.5)
    out["projU2"] = run_proj_units(2.0, 1.5)  # sensitivity, not chosen
    out["projU3"] = run_proj_units(1.0, 0.75)  # sensitivity, not chosen
    for sh in (0.25, 0.5, 1.0):
        for r2 in (0.5, 0.8):
            out[f"proj{sh}_{r2}"] = run_proj(sh, r2)
    for n in (10, 20, 30, 45):
        out[f"cross{n}s"] = run_cross_n(n)
    out["cross60"] = run_cross(True)
    out["crossInst"] = run_cross(False)
    for ws in WAITS:
        out[f"wait{ws}"] = run(False, -0.02, None, False, ws)
        out[f"waitx{ws}"] = run(False, -0.02, None, True, ws)
    for hp in HOLDS:
        out[f"hold{hp}"] = run(False, 0.0, hp)
    res.append(out)
print(f"bets {len(res)} (left out: {skipped}); as ran {sum(r['actual'] * r['n'] for r in res):+.2f}, held {sum(r['hold'] * r['n'] for r in res):+.2f}; lost {sum(1 for r in res if not r['won'])}")
print("rule: net $ | beat / lost to what ran | beat / lost to holding | net on the bets that lost (holding: net)")
for key in ["take", "value-0.05", "value-0.02", "value0.0", "value0.02", "value0.05", "both"] + ["projU", "projU2", "projU3"] + [f"proj{sh}_{r2}" for sh in (0.25, 0.5, 1.0) for r2 in (0.5, 0.8)] + ["cross10s", "cross20s", "cross30s", "cross45s", "cross60", "crossInst", "crossed0.0", "crossed0.02"] + [f"wait{w}" for w in WAITS] + [f"waitx{w}" for w in WAITS] + [f"hold{h}" for h in HOLDS]:
    net = sum(r[key] * r["n"] for r in res)
    lo = [r for r in res if not r["won"]]
    print(f"{key:9s} {net:+8.2f} | {sum(1 for r in res if r[key] > r['actual'] + 1e-9):3d} / {sum(1 for r in res if r[key] < r['actual'] - 1e-9):3d} | {sum(1 for r in res if r[key] > r['hold'] + 1e-9):3d} / {sum(1 for r in res if r[key] < r['hold'] - 1e-9):3d} | {sum(r[key] * r['n'] for r in lo):+7.2f} ({sum(r['hold'] * r['n'] for r in lo):+.2f})")
