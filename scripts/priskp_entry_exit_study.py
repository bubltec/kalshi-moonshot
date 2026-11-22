"""PriskP entry plus exit (2026-11-22; user: "E5 and E2 sound good. With some exit rules we could definitely [keep the] profits" - the message was cut off, read as: keep the profits).
QUESTION: on the same markets, the best entry rules so far, E2 (the same side passes 6 readings in a row), E5 (cushion in fifth 5 and 3 readings in a row) and E7 (cushion in fifth 5 and 6 readings in a row: the two together, stated now), held to settlement and with the always-on trail of the exit study v3. A side passes when all five checks are read and in fifths 4-5. A take at the ask, minutes 2-14, never an ask 0.45-0.55, one buy a market, fee 0.07 p (1-p), $10 a buy.
THE TRAIL (as stated in scripts/priskp_exit_study.py v3, nothing new): from the buy track the highest bid; sell at the first 10-second reading where the bid is K x u x sqrt(minutes left) under that high, u = the side's own typical minute from the bids seen since the buy (root-mean-square 10-second change times sqrt(6), read from 7 bids on); K = 1.0 stated, K = 2.0 shown as a sensitivity, not chosen. The bid at a reading is 1 minus the other side's ask (a binary market); the log ends at minute 14, so a trail is not run in the last minute (held from there). Not covered: whether a sale at that bid would fill.
Reported per entry rule: held, trail K 1.0 and K 2.0: buys, net per $1, share ahead, share sold; by buy-minute band; the halves of the days for the held version. Input: priskp_decisions.json.gz. Run: python3 scripts/priskp_entry_exit_study.py priskp_decisions.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)
half = sorted(m["close"] for m in D)[len(D) // 2]


def pick(m, rule):
    log, run = m["log"], {"yes": 0, "no": 0}
    for i, r in enumerate(log):
        s, ya, na, cy, cn = r
        for side, ask, c in (("yes", ya, cy), ("no", na, cn)):
            p = ok(c) and 0.01 <= ask <= 0.99 and not DEAD[0] <= ask <= DEAD[1]
            run[side] = run[side] + 1 if p else 0
            if not p: continue
            if rule == "E2" and run[side] >= 6: return i, side
            if rule == "E5" and c[0] == "5" and run[side] >= 3: return i, side
            if rule == "E7" and c[0] == "5" and run[side] >= 6: return i, side
    return None


def settle(m, side, price, i, k):
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    cost = price + fee(price)
    hold = ((1.0 if won else 0.0) - cost) * 10.0 / price
    if k is None: return hold, hold, False
    high, seen = 0.0, []
    for r in m["log"][i + 1:]:
        b = 1.0 - (r[2] if side == "yes" else r[1])  # the side's bid = 1 minus the other side's ask
        left = 15.0 - r[0] / 60.0
        seen.append(b); high = max(high, b)
        if len(seen) >= 7 and left > 0:
            d = [y - x for x, y in zip(seen, seen[1:])]
            u = math.sqrt(sum(v * v for v in d) / len(d)) * math.sqrt(6.0)
            if u > 0 and b <= high - k * u * math.sqrt(left): return ((b - fee(b)) - cost) * 10.0 / price, hold, True
    return hold, hold, False


def checks_exit(m, side, price, i):
    """2026-11-22 (one more stated rule, symmetric with the entry: the checks refuse a buy on a NO, so a NO sells it): sell at the first later reading where any of the five checks for the held side is in fifths 1-2 or an anomaly, at the side's bid then (1 minus the other side's ask). Unread does not sell."""
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    cost = price + fee(price)
    hold = ((1.0 if won else 0.0) - cost) * 10.0 / price
    for r in m["log"][i + 1:]:
        c = r[3] if side == "yes" else r[4]
        if any(ch in "12A" for ch in c):
            b = 1.0 - (r[2] if side == "yes" else r[1])
            return ((b - fee(b)) - cost) * 10.0 / price, True
    return hold, False


def summarize(g, idx):
    n = len(g)
    if n < 30: return "n %5d (too few)" % n
    net = sum(x[idx] for x in g)
    return "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%%  sold %2.0f%%" % (n, net, 100 * net / (10.0 * n), 100 * sum(1 for x in g if x[idx] > 0) / n, 100 * sum(1 for x in g if x[idx + 3]) / n)


for rule in ("E2", "E5", "E7"):
    rows = []
    for m in D:
        b = pick(m, rule)
        if not b: continue
        i, side = b; price = m["log"][i][1 if side == "yes" else 2]
        h, hold, sold1 = settle(m, side, price, i, 1.0)
        t1 = (h, hold, sold1)
        t2 = settle(m, side, price, i, 2.0)
        c3 = checks_exit(m, side, price, i)
        rows.append((hold, t1[0], t2[0], False, t1[2], t2[2], m["log"][i][0] / 60.0, m["close"], price, hold > 0, c3[0], c3[1]))
    # tuple: held, trail1, trail2, (unused), sold1, sold2, minute, close, price, heldwon
    rec = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    fmt = lambda idx, soldidx: (lambda g: "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%%  sold %2.0f%%" % (len(g), sum(x[idx] for x in g), 100 * sum(x[idx] for x in g) / (10.0 * len(g)), 100 * sum(1 for x in g if x[idx] > 0) / len(g), 100 * sum(1 for x in g if x[soldidx]) / len(g)) if len(g) >= 30 else "n %5d (too few)" % len(g))(rows)
    print(f"\n{rule}: buys on {len(rows)} of {len(D)} markets")
    print("  held      : " + fmt(0, 3))
    print("  trail K=1 : " + fmt(1, 4))
    print("  trail K=2 : " + fmt(2, 5) + "   (sensitivity, not chosen)")
    g3 = rows
    print("  checks say NO -> sell: " + ("n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%%  sold %2.0f%%" % (len(g3), sum(r[10] for r in g3), 100 * sum(r[10] for r in g3) / (10.0 * len(g3)), 100 * sum(1 for r in g3 if r[10] > 0) / len(g3), 100 * sum(1 for r in g3 if r[11]) / len(g3))) + "   (the stated check exit)")
    print("  held by half of the days: " + " | ".join("%s %+.1fc n%d" % (lab, 100 * sum(r[0] for r in g) / (10.0 * len(g)), len(g)) for lab, g in (("first", [r for r in rows if r[7] < half]), ("second", [r for r in rows if r[7] >= half])) if g))
    print("  by buy minute (held | trail K=1 net per $1): " + " | ".join(f"{a}-{int(b)}: " + (f"{100 * sum(r[0] for r in g) / (10.0 * len(g)):+.1f}c | {100 * sum(r[1] for r in g) / (10.0 * len(g)):+.1f}c n{len(g)}" if len(g) >= 30 else "few") for a, b in ((2, 4), (4, 7), (7, 10), (10, 12), (12, 14.01)) for g in [[r for r in rows if a <= r[6] < b]]))
