"""At what size does a swing reading follow the curve a bet rides? (2026-11-26; user, on the wave exit that sold winners for a cent: "Sounds like it didn't follow any curves correctly"; on rebuilding the reading at the market's own wave size and checking it before using it: "Yes".)
THE FINDING THIS FOLLOWS: the entry's leg reading (wavefacts.leg: a swing of 0.5 typical minutes, a turn at 0.2 given back) changed its mind a median of 9 times on a bet that went on to win and read 'turned' or 'against' for 54% of the time such a bet was held; its turn is half the spot's typical 10 s move.
QUESTION (one, on the same bets): the spot (the settlement index, a trailing 10 s mean, from 30 minutes before the open) is cut into swings by a zig-zag whose REVERSAL SIZE is one of a few sizes read from the market itself (the hour before the open). For each size: on the bets that settled the bet's way, how often does the reading change while the bet is held and what share of that time does it say the swing runs WITH the bet; on the bets that settled against, how soon after the buy does it first say AGAINST and what was the bid then. Then the reading used as the profit side of the one rule (a bet ahead after both fees is sold when its swing runs against it; the loss side, cushion hold and top unchanged), against holding, the deployed bid-fall rule and what happened.
THE SIZES (stated before the run, none fitted): 0.5 u (what the entry's reading uses), 1 u, 2 u (u = the spot's typical minute over the hour before the open); w3 and w10 (how far this market's spot usually travels in 3 and in 10 minutes, the root-mean-square change over the hour before the open); tau (its usual travel over its own wave time, the decay time of the last 30 minutes before the open, where one shows).
HOW TO READ IT: a reading follows the curve when winners are read WITH for most of the hold with few changes AND losers are read AGAINST early, while the bid is still near the price paid. A size that never turns scores well on the first and fails the second. The money lines have no buy-back and no fill model; a day and a half of bets; a lead.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_swing_scale_study.py BETS.json.gz INDEX.json.gz"""
import bisect
import gzip
import json
import math
import re
import statistics
import sys

from kalshi_moonshot import fallrule, ticks, wavefacts, waveturn
from kalshi_moonshot.kalshiws import index_for_series

TICK = 0.01
bets = json.load(gzip.open(sys.argv[1]))
tape = json.load(gzip.open(sys.argv[2]))
ticks.history.keep = ticks.history.slack = 1e12
for ix, (T, V) in tape.items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))


def fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def path(ix: str, a: int, b: int) -> list[float] | None:
    """The index on a 1 s grid a..b as a trailing 10 s mean (the last tick carried forward); None on a gap at the start."""
    T, V = tape[ix]
    k = bisect.bisect_right(T, a) - 1
    if k < 0:
        return None
    raw, last = [], V[k]
    k += 1
    for s in range(a, b + 1):
        while k < len(T) and T[k] <= s:
            last = V[k]
            k += 1
        raw.append(last)
    out, run = [], 0.0
    for i, v in enumerate(raw):
        run += v - (raw[i - 10] if i >= 10 else 0.0)
        out.append(run / min(i + 1, 10))
    return out


def directions(xs: list[float], rev: float) -> list[int]:
    """The zig-zag's direction at every second (+1 up, -1 down, 0 before the first swing of `rev`)."""
    out, d, lo, hi, ev = [0], 0, xs[0], xs[0], xs[0]
    for v in xs[1:]:
        if d == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= rev:
                d, ev = 1, v
            elif hi - v >= rev:
                d, ev = -1, v
        elif (v - ev) * d > 0:
            ev = v
        elif (ev - v) * d >= rev:
            d, ev = -d, v
        out.append(d)
    return out


def sizes(ix: str, opened: int) -> dict[str, float | None]:
    h = waveturn._hour(ix, opened)
    bef = wavefacts.before(ix, float(opened))
    u = h.u
    tau = bef.get("tau_s") if bef else None
    return {
        "0.5 u (entry's)": None if not u else 0.5 * u, "1 u": u, "2 u": None if not u else 2.0 * u,
        "w3 (3 min travel)": bef.get("w3") if bef else None, "w10 (10 min travel)": bef.get("w10") if bef else None,
        "tau travel": wavefacts.travel_left(bef, float(tau)) if (bef and tau) else None,
    }


NAMES = ["0.5 u (entry's)", "1 u", "2 u", "w3 (3 min travel)", "w10 (10 min travel)", "tau travel"]
stat = {n: {"flips": [], "with": [], "lead": [], "bid_at": [], "never": 0, "losers": 0, "money": 0.0, "sold": 0, "sold_net": [], "n": 0, "in_u": []} for n in NAMES}
base = {"actual": 0.0, "held": 0.0, "fall": 0.0, "n": 0}
for b in bets:
    close, side, entry, strike = b["close"], b["side"], b["entry"], b["strike"]
    if len(b["bid"]) != 900 or len(b["idx"]) != 1520 or not strike or b["result"] not in ("yes", "no"):
        continue
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = close - 900
    xs = path(ix, opened - 1800, close)
    sz = sizes(ix, opened)
    if xs is None or not sz["1 u"]:
        continue
    want = 1 if side == "yes" else -1
    won = b["result"] == side
    sb = [None if (x is None or y is None) else (x if side == "yes" else 1.0 - y) for x, y in zip(b["bid"], b["ask"], strict=True)]
    mid = [None if (x is None or y is None) else (x + y) / 2.0 for x, y in zip(b["bid"], b["ask"], strict=True)]
    sq, cnt = [0.0], [0]
    for a, c in zip(mid, mid[1:], strict=False):
        ok = a is not None and c is not None
        sq.append(sq[-1] + ((c - a) ** 2 if ok else 0.0))
        cnt.append(cnt[-1] + (1 if ok else 0))
    idx, i0 = b["idx"], close - 1520
    isq, icnt, isum, iok = [0.0], [0], [0.0], [0]
    for k, v in enumerate(idx):
        p = idx[k - 1] if k else None
        ok = v is not None and p is not None
        isq.append(isq[-1] + ((v - p) ** 2 if ok else 0.0))
        icnt.append(icnt[-1] + (1 if ok else 0))
        isum.append(isum[-1] + (v or 0.0))
        iok.append(iok[-1] + (1 if v is not None else 0))
    k0 = max(int(math.ceil(b["ts"])) + 1 - (opened + 1), 1)

    def money(dirs: list[int] | None) -> tuple[float, bool, float]:
        """The bet's result under the one rule, its profit side the bid fall (dirs None) or the swing reading; (result a contract, sold?, net at the sale)."""
        st = fallrule.State()
        for k in range(k0, 899):
            now = opened + 1 + k
            x = sb[k]
            if x is None or x <= 0:
                continue
            left = close - now
            unit = math.sqrt(60.0 * (sq[k] - sq[0]) / cnt[k]) if (k >= 90 and cnt[k] >= 60 and sq[k] > 0) else None
            wob = max(TICK, unit * math.sqrt(10.0 / 60.0)) if unit else TICK
            j = now - (i0 + 1)
            cushion = sunit = None
            if j >= 600 and iok[j + 1] - iok[j - 59] == 60 and icnt[j + 1] - icnt[j - 599] >= 60:
                sunit = math.sqrt(60.0 * (isq[j + 1] - isq[j - 599]) / (icnt[j + 1] - icnt[j - 599]))
                cushion = ((isum[j + 1] - isum[j - 59]) / 60.0 - strike) * want
            wave: object = "off"
            if dirs is not None:
                d = dirs[1800 + 1 + k]
                wave = ("against" if d == -want else "inside", False)
            r = fallrule.decide(st, x, entry, fee(x), fee(entry), fee, wob, now, left, cushion, sunit, None, None, b["ts"], wave)
            if r.sell:
                return x - fee(x) - entry, r.kind == "turn", r.net
        return (1.0 if won else 0.0) - entry, False, 0.0

    base["n"] += 1
    base["actual"] += ((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) * b["n"] - entry * b["n"]
    base["held"] += ((1.0 if won else 0.0) - entry) * b["n"]
    base["fall"] += money(None)[0] * b["n"]
    for name in NAMES:
        rev = sz[name]
        if not rev:
            continue
        s = stat[name]
        s["n"] += 1
        s["in_u"].append(rev / sz["1 u"])
        dirs = directions(xs, rev)
        held = [dirs[1800 + 1 + k] * want for k in range(k0, 899)]
        if won:
            s["flips"].append(sum(1 for a, c in zip(held, held[1:], strict=False) if a != c))
            s["with"].append(sum(1 for v in held if v > 0) / len(held))
        else:
            s["losers"] += 1
            first = next((i for i, v in enumerate(held) if v < 0), None)
            if first is None:
                s["never"] += 1
            else:
                s["lead"].append(first)
                x = sb[k0 + first]
                if x is not None:
                    s["bid_at"].append(x - entry)
        m, sold, net = money(dirs)
        s["money"] += m * b["n"]
        if sold:
            s["sold"] += 1
            s["sold_net"].append(net)

print(f"{base['n']} live PriskP bets. actual ${base['actual']:.2f}   held to settlement ${base['held']:.2f}   one rule with the bid-fall profit side (deployed) ${base['fall']:.2f}\n")
print(f"{'reversal size':22s} {'n':>4s} {'in u':>5s} | winners: {'changes':>7s} {'time WITH':>9s} | losers: {'never AGAINST':>13s} {'first AGAINST after':>19s} {'bid then vs paid':>16s} | as the profit side: {'total':>9s} {'turn sales':>10s} {'kept a sale':>11s}")
for name in NAMES:
    s = stat[name]
    if not s["n"]:
        continue
    med = lambda v: statistics.median(v) if v else float("nan")  # noqa: E731
    print(f"{name:22s} {s['n']:4d} {med(s['in_u']):5.1f} | winners: {med(s['flips']):7.0f} {med(s['with']):9.0%} | losers: {s['never']:6d} of {s['losers']:3d} {med(s['lead']):17.0f} s {med(s['bid_at']) * 100:+14.0f}c | as the profit side: ${s['money']:8.2f} {s['sold']:10d} {med(s['sold_net']) * 100:+10.1f}c")
