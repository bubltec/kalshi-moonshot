"""PriskP exit study: the user's wave_take rules (2026-11-22, the user's own specification, taken as written).
THE RULES (the user's): on any bet that is AHEAD AFTER BOTH FEES (bid - entry > entry fee + sell fee; otherwise nothing is sold, the bet is held): (1) [REMOVED by the user, 2026-11-22: "you could remove the 40c rule, that's the scalp part"] (2) Spot average NOT on the bet's side of the target (or unreadable): sell at the first one-cent turn down from the high (the bid at or under the highest bid since the buy minus 0.01; a flat bid does not count; no 20c minimum). (3) Winning side (the spot average on the bet's side): trail the high with a giveback of TRAIL_K x the bid's typical minute x sqrt(time left / 60); it shrinks to nothing as the close approaches. (4) Bid of 0.80 or more: sell when P_lose >= 1 - bid + fee (the sell fee at that bid). Otherwise hold toward 100c (settlement).
MY CHOICES (named): TRAIL_K = 2.0 (the user gave no number; 1.0 and 3.0 shown as sensitivities, not chosen); the spot average's side = the sign of the blended 1/3/5-minute cushion (binaryrisk's czb, in the log); P_lose = Phi(-side cushion), the normal chance of ending on the other side given the time-scaled cushion (the blended cushion is already in typical moves over the time left): no other lane's chance is used; the typical minute = the rms 10-second change of the bet's bid over the log's bids up to now (at most the last 15 minutes) times sqrt(6), read from 7 changes on; the bid = 1 minus the other side's ask; the log is every 10 s and ends at minute 14, so nothing is sold in the last minute (held from there); a sale is at that reading's bid; fills are not modelled. Rule order when several hold: (2) or (3) by the spot's side, and (4) in either state. Rule 4 is also shown switched off (P_LOSE=0), because my P_lose is a stand-in.
ENTRIES (scripts/priskp_entry_study.py): E0 first pass, E2 six readings in a row, E5 cushion fifth 5 and three in a row, E7 cushion fifth 5 and six in a row; a take at the ask, minutes 2-14, never an ask 0.45-0.55, one buy a market. Held to settlement is the comparison. Input: priskp_decisions.json.gz with the cushion in each log entry. Run: python3 scripts/priskp_wave_take_study.py priskp_decisions.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
import os
USE_PLOSE = os.environ.get("P_LOSE", "1") != "0"
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)


def pick(m, rule):
    log, run = m["log"], {"yes": 0, "no": 0}
    for i, r in enumerate(log):
        s, ya, na, cy, cn = r[:5]
        for side, ask, c in (("yes", ya, cy), ("no", na, cn)):
            p = ok(c) and 0.01 <= ask <= 0.99 and not DEAD[0] <= ask <= DEAD[1]
            run[side] = run[side] + 1 if p else 0
            if not p: continue
            if rule == "E0" or (rule == "E2" and run[side] >= 6) or (rule == "E5" and c[0] == "5" and run[side] >= 3) or (rule == "E7" and c[0] == "5" and run[side] >= 6): return i, side
    return None


def trade(m, i, side, K):
    log = m["log"]
    entry = log[i][1] if side == "yes" else log[i][2]
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    fe = fee(entry)
    hold = ((1.0 if won else 0.0) - entry - fe) * 10.0 / entry
    sg = 1.0 if side == "yes" else -1.0
    bids = [1.0 - (r[2] if side == "yes" else r[1]) for r in log]
    high = 0.0
    for j in range(i + 1, len(log)):
        b = bids[j]; high = max(high, b)
        T = 900.0 - log[j][0]
        if T <= 60.0: continue
        if not b - entry > fe + fee(b): continue  # only a bet that is ahead after both fees is sold
        z = sg * log[j][5]
        reason = None
        if z <= 0:
            if b <= high - 0.01 - 1e-9 and high > bids[i] - 1e-9: reason = "turn"
        else:
            d = [y - x for x, y in zip(bids[:j], bids[1:j + 1])]
            if len(d) >= 7:
                u = math.sqrt(sum(v * v for v in d[-90:]) / len(d[-90:])) * math.sqrt(6.0)
                if u > 0 and b <= high - K * u * math.sqrt(T / 60.0): reason = "trail"
        if reason is None and USE_PLOSE and b >= 0.80 and (1.0 - phi(z)) >= 1.0 - b + fee(b): reason = "p_lose"
        if reason: return ((b - fee(b)) - entry - fe) * 10.0 / entry, hold, reason
    return hold, hold, "settled"


def fmt(g, ix):
    n = len(g)
    if n < 30: return "n %5d (too few)" % n
    by = {}
    for x in g: by[x[ix][2]] = by.get(x[ix][2], 0) + 1
    return "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%%  ended: %s" % (n, sum(x[ix][0] for x in g), 100 * sum(x[ix][0] for x in g) / (10.0 * n), 100 * sum(1 for x in g if x[ix][0] > 0) / n, " ".join(f"{k} {v}" for k, v in sorted(by.items())))


for rule in ("E0", "E2", "E5", "E7"):
    rows = []
    for m in D:
        b = pick(m, rule)
        if not b: continue
        i, side = b
        a = trade(m, i, side, 2.0)
        rows.append({"held": (a[1], a[1], "held"), "stated": a, "k1": trade(m, i, side, 1.0), "k3": trade(m, i, side, 3.0), "minute": m["log"][i][0] / 60.0, "close": m["close"]})
    half = sorted(r["close"] for r in rows)[len(rows) // 2]
    print(f"\n{rule}: buys on {len(rows)} of {len(D)} markets")
    print("  held                   : " + fmt(rows, "held"))
    print("  wave_take (TRAIL_K 2.0): " + fmt(rows, "stated"))
    print("    sensitivity, not chosen, TRAIL_K 1.0: " + fmt(rows, "k1") + "\n    sensitivity, not chosen, TRAIL_K 3.0: " + fmt(rows, "k3"))
    print("  halves of the days (held | wave_take): " + " | ".join("%s %+.1fc | %+.1fc n%d" % (lab, 100 * sum(r["held"][0] for r in g) / (10.0 * len(g)), 100 * sum(r["stated"][0] for r in g) / (10.0 * len(g)), len(g)) for lab, g in (("first", [r for r in rows if r["close"] < half]), ("second", [r for r in rows if r["close"] >= half]))))
    print("  by buy minute (held | wave_take): " + " | ".join(f"{a}-{int(b)}: " + (f"{100 * sum(r['held'][0] for r in g) / (10.0 * len(g)):+.1f}c | {100 * sum(r['stated'][0] for r in g) / (10.0 * len(g)):+.1f}c n{len(g)}" if len(g) >= 30 else "few") for a, b in ((2, 4), (4, 7), (7, 10), (10, 12), (12, 14.01)) for g in [[r for r in rows if a <= r["minute"] < b]]))
