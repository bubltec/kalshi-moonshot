"""PriskP exit study: the user's staircase and high-zone exit (2026-11-22, the user's own specification, from the "Recent scalp wins" session, taken as written).
DEFINITIONS (the user's): bid = the bet's current bid, peak = its highest bid since the buy, T = seconds left; u = the bid's typical minute (rms one-second change over the last 15 minutes times sqrt(60); the log is every 10 s so: rms 10-second change times sqrt(6) over the last up to 90 changes in the log, from the log's start at minute 2); net = bid - fee(bid) - entry - fee(entry) per contract, fee = 0.07 p (1-p).
RULE 1, THE STAIRCASE: rev = max(0.01, 0.25 u sqrt(max(T, 60) / 60)). After a high, a pullback of rev confirms the SWING HIGH; after that, a bounce of rev off the lowest point confirms a SWING LOW. Sell when the bid falls under the last confirmed swing low and the bet has been ahead at some point (max net > 0 since the buy); the sale can be at a gain or a small loss. No loss cap.
THE SECOND SESSION'S LATER FORM (read from the "Recent scalp wins" session, where the user said yes to it, 2026-11-22): rev(T) = max(0.01, k0 u (max(T, 60)/60)^p g), with g = 2 Phi(z) = 2 (1 - P_lose), z = the spot's cushion over u_s sqrt(T/60) (the same z as rule 2), g = 1 when the spot cannot be read; defaults k0 = 0.25, p = 0.5, the cushion on. The study shows the stated form (g = 1) and this form (g on) side by side.
RULE 2, THE HIGH ZONE: when bid >= 0.80 and net > 0: P_lose = Phi(-cushion / (u_s sqrt(T / 60))), cushion = the spot's 60-second average minus the target, flipped for a NO bet; sell when P_lose >= 1 - bid + fee(bid); otherwise hold toward 100c. MY COMPUTATION of the cushion over u_s sqrt(T / 60): binaryrisk's 60-second-average cushion in the spot's typical minutes over the time left (cz1 in the log: ln(average / strike) / (u15 sqrt(minutes left)), u15 the spot's rms per-minute move over 15 minutes), which is the same ratio in log units.
MY CHOICES (named): the first swing high starts from the highest bid since the buy; the bid = 1 minus the other side's ask; the log ends at minute 14, so nothing is sold in the last minute (held from there); a sale is at that reading's bid; fills are not modelled; 0.25 is the user's number (he says not final: the replay over every wave bet is owed), 0.5 and 1.0 shown as sensitivities, not chosen.
ENTRIES (scripts/priskp_entry_study.py): E0 first pass, E2 six readings in a row, E5 cushion fifth 5 and three in a row, E7 cushion fifth 5 and six in a row; a take at the ask, minutes 2-14, never an ask 0.45-0.55, one buy a market. Held to settlement is the comparison.
Reported: held, rule 1 alone, rule 2 alone, both; the staircase at 0.5 and 1.0; by buy minute and the halves of the days. Input: priskp_decisions.json.gz with cz1 in each log entry. Run: python3 scripts/priskp_staircase_study.py priskp_decisions.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
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


def trade(m, i, side, mult, use1, use2, pw=0.5, useg=False):
    log = m["log"]
    entry = log[i][1] if side == "yes" else log[i][2]
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    fe = fee(entry)
    hold = ((1.0 if won else 0.0) - entry - fe) * 10.0 / entry
    sg = 1.0 if side == "yes" else -1.0
    bids = [1.0 - (r[2] if side == "yes" else r[1]) for r in log]
    phase, hi, lo, swing_low, maxnet = "up", 0.0, 1.0, None, -9.0
    for j in range(i + 1, len(log)):
        b = bids[j]; T = 900.0 - log[j][0]
        if T <= 60.0: break
        net = b - fee(b) - entry - fe
        maxnet = max(maxnet, net)
        d = [y - x for x, y in zip(bids[:j], bids[1:j + 1])]
        u = math.sqrt(sum(v * v for v in d[-90:]) / len(d[-90:])) * math.sqrt(6.0) if len(d) >= 3 else 0.0
        zz = sg * (log[j][6] if len(log[j]) > 6 else log[j][5])
        g = 2.0 * phi(zz) if useg else 1.0
        rev = max(0.01, mult * u * (max(T, 60.0) / 60.0) ** pw * g)
        # the staircase
        if phase == "up":
            hi = max(hi, b)
            if b <= hi - rev: phase, lo = "down", b
        else:
            lo = min(lo, b)
            if b >= lo + rev: swing_low, phase, hi = lo, "up", b
        if use1 and swing_low is not None and b < swing_low and maxnet > 0: return net * 10.0 / entry, hold, "stair"
        if use2 and b >= 0.80 and net > 0 and len(d) >= 3:
            z = sg * log[j][6] if len(log[j]) > 6 else sg * log[j][5]
            if (1.0 - phi(z)) >= 1.0 - b + fee(b): return net * 10.0 / entry, hold, "p_lose"
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
        a = trade(m, i, side, 0.25, True, True)
        rows.append({"held": (a[1], a[1], "held"), "both": a, "r1": trade(m, i, side, 0.25, True, False), "r2": trade(m, i, side, 0.25, False, True), "s05": trade(m, i, side, 0.5, True, False), "s10": trade(m, i, side, 1.0, True, False), "g": trade(m, i, side, 0.25, True, False, 0.5, True), "g_both": trade(m, i, side, 0.25, True, True, 0.5, True), "g50": trade(m, i, side, 0.5, True, False, 0.5, True), "p1": trade(m, i, side, 0.25, True, False, 1.0, False), "p0": trade(m, i, side, 0.25, True, False, 0.0, False), "minute": m["log"][i][0] / 60.0, "close": m["close"]})
    half = sorted(r["close"] for r in rows)[len(rows) // 2]
    print(f"\n{rule}: buys on {len(rows)} of {len(D)} markets")
    print("  held                  : " + fmt(rows, "held"))
    print("  rule 1 staircase (0.25): " + fmt(rows, "r1"))
    print("  rule 2 high zone      : " + fmt(rows, "r2"))
    print("  both                  : " + fmt(rows, "both"))
    print("  rule 1 with the cushion factor g (0.25, p 0.5): " + fmt(rows, "g") + "\n  both rules with g      : " + fmt(rows, "g_both"))
    print("    sensitivity, not chosen: g with k0 0.5: " + fmt(rows, "g50") + "\n                             p 1.0 (no g): " + fmt(rows, "p1") + "\n                             p 0.0 (no g): " + fmt(rows, "p0"))
    print("    sensitivity, not chosen, staircase 0.5: " + fmt(rows, "s05") + "\n    sensitivity, not chosen, staircase 1.0: " + fmt(rows, "s10"))
    print("  halves of the days (held | both): " + " | ".join("%s %+.1fc | %+.1fc n%d" % (lab, 100 * sum(r["held"][0] for r in g) / (10.0 * len(g)), 100 * sum(r["both"][0] for r in g) / (10.0 * len(g)), len(g)) for lab, g in (("first", [r for r in rows if r["close"] < half]), ("second", [r for r in rows if r["close"] >= half]))))
    print("  by buy minute (held | both): " + " | ".join(f"{a}-{int(b)}: " + (f"{100 * sum(r['held'][0] for r in g) / (10.0 * len(g)):+.1f}c | {100 * sum(r['both'][0] for r in g) / (10.0 * len(g)):+.1f}c n{len(g)}" if len(g) >= 30 else "few") for a, b in ((2, 4), (4, 7), (7, 10), (10, 12), (12, 14.01)) for g in [[r for r in rows if a <= r["minute"] < b]]))
