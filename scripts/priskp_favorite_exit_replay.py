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


MARGINS = (-0.05, -0.02, 0.0, 0.02, 0.05)
HOLDS = (0.005, 0.01, 0.02, 0.05, 0.10)  # the hold step: no value sale while p_lose is at or under this bar (2026-11-23, user: hold the clearly safe ones for the small gain); a bar is a chance, shown at five levels, none chosen yet
res, skipped = [], 0
for b in data["bets"]:
    t0, close, n = float(b["ts"]), float(b["close_ts"]), float(b["contracts"])
    basis, side = float(b["stake_usd"]) / n, b["side"]
    q, sp = data["quotes"][b["ticker"]], spot.get(b["ticker"])
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

    def hold_value(sec):
        if sec not in cache:
            k = sum(1 for x in T if x <= sec)  # ticks up to now only
            r = wavetop.spot_read(T[:k], V[:k], float(sec), float(sp["strike"]), side) if k else None
            cache[sec] = None if r is None else 1.0 - wavetop.p_lose(r[0], r[1], close - sec)
        return cache[sec]

    def lose_now(sec):
        hv = hold_value(sec)
        return None if hv is None else 1.0 - hv

    def run(take, value_margin, hold_p=None):
        for sec, bid in grid:
            if sec <= t0:
                continue
            net = bid - fee(bid) - basis - fee(basis)
            if take and net >= 0.5 * u:
                return net
            if value_margin is not None and sec % 5 == 0:
                hv = hold_value(sec)
                pl = lose_now(sec)
                if hold_p is not None and pl is not None and pl <= hold_p:
                    continue  # the hold step: a bet clearly safe by the spot is not asked the value sale
                if hv is not None and bid - fee(bid) >= hv - value_margin:
                    return net
        return held

    out = {"actual": actual, "hold": held, "n": n, "won": won, "take": run(True, None), "both": run(True, 0.0)}
    for m in MARGINS:
        out[f"value{m}"] = run(False, m)
    for hp in HOLDS:
        out[f"hold{hp}"] = run(False, 0.0, hp)
    res.append(out)
print(f"bets {len(res)} (left out: {skipped}); as ran {sum(r['actual'] * r['n'] for r in res):+.2f}, held {sum(r['hold'] * r['n'] for r in res):+.2f}; lost {sum(1 for r in res if not r['won'])}")
print("rule: net $ | beat / lost to what ran | beat / lost to holding | net on the bets that lost (holding: net)")
for key in ["take", "value-0.05", "value-0.02", "value0.0", "value0.02", "value0.05", "both"] + [f"hold{h}" for h in HOLDS]:
    net = sum(r[key] * r["n"] for r in res)
    lo = [r for r in res if not r["won"]]
    print(f"{key:9s} {net:+8.2f} | {sum(1 for r in res if r[key] > r['actual'] + 1e-9):3d} / {sum(1 for r in res if r[key] < r['actual'] - 1e-9):3d} | {sum(1 for r in res if r[key] > r['hold'] + 1e-9):3d} / {sum(1 for r in res if r[key] < r['hold'] - 1e-9):3d} | {sum(r[key] * r['n'] for r in lo):+7.2f} ({sum(r['hold'] * r['n'] for r in lo):+.2f})")
