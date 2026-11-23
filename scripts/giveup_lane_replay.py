"""The salvage GIVE-UP replayed on the lane's OWN bets (2026-11-23; user: "if it dips and doesn't turn we should exit", "the earlier in the market, the less we should wait").

Why: the first live give-up sales (share 0.3) were all losers, but sold at 2 to 7c after the bid had collapsed, so they saved almost nothing. This replays the rule on the real quote paths of the wave lanes' own settled or sold bets since 2026-11-22 18:00 UTC (real fills at the decision ask, the side's bid as the sale price, both fees), at several wait shares, to choose the share from data and not from a guess. The rule is the one in src/kalshi_moonshot/wavetop.py `salvage`: a bet is ARMED when its side bid falls one of its own typical minutes (qu, the rms 60 s change of the side bid before the buy) under the bid at the buy; the clock runs from the later of the arming and the last TURN (the bid back half a qu off the lowest bid since the buy); it gives up when it has waited W = share x min(seconds since the open, seconds left x p / (1 - p)), p = 1/3. A lane sale earlier than the give-up stands (the lane's actual exit); otherwise the give-up sale; otherwise the bet is held to settlement. Share 0 = sell at the arming, with no wait. The spot-on-its-side skip is NOT replayed (no spot here), so a flipped winner could be given up in this replay that the live step would hold. Stake is taken as $7.50 a bet (the lane's fixed stake). Read-only.

Inputs (written by the fetch scripts): bets.txt ("B id ticker side ask ts close outcome exit_price exit_ts") and q2.txt ("Q id off:bid:ask,off:bid:ask,..." in thousandths, off = seconds after the market's open).
    .venv/bin/python scripts/giveup_lane_replay.py DIR
"""

import math
import sys
from pathlib import Path

D = Path(sys.argv[1])
STAKE = 7.5


def fee1(p):
    return 0.07 * p * (1 - p)


bets = {}
for ln in (D / "bets.txt").read_text().splitlines():
    p = ln.split()
    if len(p) < 10:
        continue
    _, bid_, tk, side, ask, ts, cl, o, ex, exts = p[:10]
    bets[int(bid_)] = {"tk": tk, "side": side, "ask": float(ask), "ts": float(ts), "cl": float(cl), "o": o if o != "-" else "", "ex": float(ex) if ex != "-" else None, "exts": float(exts) if exts != "-" else None}

paths = {}
for ln in (D / "q2.txt").read_text().splitlines():
    p = ln.split()
    if len(p) < 3:
        continue
    paths[int(p[1])] = p[2]


def grid(raw):
    """Second-by-second YES bid and ask over the 900 s market, the last quote carried forward."""
    rows = []
    for tok in raw.split(","):
        try:
            off, b, a = tok.split(":")
            rows.append((int(off), int(b) / 1000.0, int(a) / 1000.0))
        except ValueError:
            continue
    if not rows:
        return None
    yb, ya = [None] * 901, [None] * 901
    for off, b, a in rows:
        if 0 <= off <= 900:
            yb[off], ya[off] = b, a
    first = next((i for i in range(901) if yb[i] is not None), None)
    if first is None:
        return None
    last_b, last_a = yb[first], ya[first]
    for i in range(901):
        if yb[i] is None:
            yb[i], ya[i] = last_b, last_a
        else:
            last_b, last_a = yb[i], ya[i]
    return yb, ya


def side_bid(yb, ya, side):
    return list(yb) if side == "yes" else [1.0 - x for x in ya]


def run(bet, sb, share, turn=0.5, peak=1.0 / 3.0):
    """(sale second or None, sale bid, qu info) for the give-up on this bet; None when never armed or no unit."""
    open_ = bet["cl"] - 900.0
    i0 = int(bet["ts"] - open_)
    if i0 < 60 or i0 >= 899:
        return None
    ch = [sb[k] - sb[k - 60] for k in range(60, i0, 5)]
    if not ch:
        return None
    qu = max(math.sqrt(sum(c * c for c in ch) / len(ch)), 0.01)
    b0 = sb[i0]
    armed, iarm, iturn, lo_run = False, -1, -1, b0
    for j in range(i0 + 1, 900):
        if sb[j] < b0 - qu and not armed:
            armed, iarm = True, j
        if sb[j] < lo_run:
            lo_run = sb[j]
        elif armed and sb[j] - lo_run >= turn * qu:
            iturn = j
        if armed:
            left = max(900 - j, 0)
            w = share * min(j, left * peak / (1.0 - peak))
            since = max(iarm, iturn)
            if j - since >= w:
                return j, sb[j], qu
    return None


def pnl_sale(ask, x):
    c = STAKE / ask
    return c * (x - ask) - c * fee1(ask) - c * fee1(x)


def pnl_hold(ask, won):
    c = STAKE / ask
    return c * ((1 if won else 0) - ask) - c * fee1(ask)


rows = []
for i, b in bets.items():
    raw = paths.get(i)
    if not raw:
        continue
    g = grid(raw)
    if not g:
        continue
    sb = side_bid(*g, b["side"])
    won = b["o"] == b["side"]
    settled = b["o"] in ("yes", "no")
    if not settled and b["ex"] is None:
        continue
    exoff = int(b["exts"] - (b["cl"] - 900.0)) if b["exts"] else None
    actual = pnl_sale(b["ask"], b["ex"]) if b["ex"] is not None else pnl_hold(b["ask"], won)
    hold = pnl_hold(b["ask"], won) if settled else None
    rows.append((i, b, sb, won, exoff, actual, hold))

print(f"{len(rows)} bets with a quote path of {len(bets)} (stake ${STAKE} a bet)")
held = [r for r in rows if r[6] is not None]
print(f"lane's actual result {sum(r[5] for r in rows):8.1f} | held to settlement ({len(held)} settled bets) {sum(r[6] for r in held):8.1f}\n")
print(f"{'share':>6} {'fires':>5} {'on losers':>9} {'on winners':>10} {'mean sale bid':>13} | {'actual + give-up':>16} {'(vs actual)':>11} | {'give-up on every bet it fires on, vs holding those':>50}")
for share in (0.0, 0.02, 0.05, 0.1, 0.15, 0.3):
    total, fires, lose_n, win_n, bids, d_vs_hold, d_vs_actual = 0.0, 0, 0, 0, [], 0.0, 0.0
    for i, b, sb, won, exoff, actual, hold in rows:
        res = run(b, sb, share)
        if res is None or (exoff is not None and exoff <= res[0]):
            total += actual  # the lane's own exit came first, or no give-up
            continue
        j, x, qu = res
        sale = pnl_sale(b["ask"], x)
        total += sale
        fires += 1
        bids.append(x)
        if won:
            win_n += 1
        else:
            lose_n += 1
        d_vs_actual += sale - actual
        if hold is not None:
            d_vs_hold += sale - hold
    print(f"{share:6.2f} {fires:5d} {lose_n:9d} {win_n:10d} {(sum(bids) / len(bids) if bids else float('nan')):13.3f} | {total:16.1f} {d_vs_actual:+11.1f} | {d_vs_hold:+50.1f}")
