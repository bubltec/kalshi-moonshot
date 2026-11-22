"""PriskP shift study (2026-11-22; user: a shift happens at any size and time frame, holds about 3-5 minutes to count, can repeat or last a long while).
QUESTION: on the same entries, do buys made after a confirmed shift of the spot against the side do worse than buys after a shift for the side, or with no recent shift? A shift (priskp.shifts): the spot's level after a step differs from the level before by more than noise over the hold explains (>= 2 typical minutes x sqrt(3)) and both levels held about 3 minutes; the run logged the confirmed shifts of the last hour every 30 s; a buy reads the latest log entry at or before it. In the side's view: a shift DOWN for a YES bet, or UP for a NO bet, is AGAINST the side. GROUPS (stated before the run, not tuned): the LATEST confirmed shift is against the side and at most 20 minutes old; for the side and at most 20 minutes old; older than 20 minutes (either way); none in the hour. Also the same by size (the step in typical minutes: under 6, 6 or more) and by whether it still holds (held 0.5 or more). The 20 minutes and the 6 are mine, chosen before looking. Entries as in scripts/priskp_entry_study.py (E0 first pass, E5 strong cushion and 3 readings), a take at the ask, held to settlement, fee 0.07 p (1-p), $10 a buy, never an ask 0.45-0.55, minutes 2-14, one buy a market.
Input: priskp_decisions.json.gz with the shifts in each log entry. Run: python3 scripts/priskp_shift_study.py priskp_decisions.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)
half = sorted(m["close"] for m in D)[len(D) // 2]


def pick(m, rule):
    log, run = m["log"], {"yes": 0, "no": 0}
    for i, r in enumerate(log):
        s, ya, na, cy, cn = r[:5]
        for side, ask, c in (("yes", ya, cy), ("no", na, cn)):
            p = ok(c) and 0.01 <= ask <= 0.99 and not DEAD[0] <= ask <= DEAD[1]
            run[side] = run[side] + 1 if p else 0
            if not p: continue
            if rule == "E0" or (rule == "E5" and c[0] == "5" and run[side] >= 3): return i, side
    return None


def latest_shift(m, i, side):
    sg = 1.0 if side == "yes" else -1.0
    for j in range(i, -1, -1):
        r = m["log"][j]
        if len(r) > 7 and r[7] is not None:
            sh = r[7]
            if not sh: return None
            ago, step, held = sh[-1]  # the newest confirmed shift
            return {"age": (ago + (m["log"][i][0] - r[0])) / 60.0, "dir": sg * (1 if step > 0 else -1), "size": abs(step), "held": held}
    return None


def row(m, i, side):
    log = m["log"]; price = log[i][1] if side == "yes" else log[i][2]
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    return {"price": price, "won": won, "net": ((1.0 if won else 0.0) - price - fee(price)) * 10.0 / price, "close": m["close"], "sh": latest_shift(m, i, side)}


def line(g):
    n = len(g)
    if n < 30: return "n %5d (too few)" % n
    w = sum(r["won"] for r in g) / n; nd = sum(r["price"] + fee(r["price"]) for r in g) / n
    h1, h2 = [r for r in g if r["close"] < half], [r for r in g if r["close"] >= half]
    hs = " halves %+.1fc / %+.1fc" % tuple(100 * sum(r["net"] for r in h) / (10.0 * len(h)) if h else 0.0 for h in (h1, h2))
    return "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%% need %2.0f%% %s |%s" % (n, sum(r["net"] for r in g), 100 * sum(r["net"] for r in g) / (10.0 * n), 100 * w, 100 * nd, "BEAT" if w > nd else "no  ", hs)


for rule in ("E0", "E5"):
    rows = []
    for m in D:
        b = pick(m, rule)
        if b: rows.append(row(m, *b))
    sh = lambda r: r["sh"]
    print(f"\n{rule}: {len(rows)} buys")
    print("  all                                  : " + line(rows))
    print("  no confirmed shift in the hour       : " + line([r for r in rows if sh(r) is None]))
    print("  latest shift AGAINST the side, <= 20 min : " + line([r for r in rows if sh(r) and sh(r)["dir"] < 0 and sh(r)["age"] <= 20]))
    print("  latest shift FOR the side, <= 20 min     : " + line([r for r in rows if sh(r) and sh(r)["dir"] > 0 and sh(r)["age"] <= 20]))
    print("  latest shift older than 20 min           : " + line([r for r in rows if sh(r) and sh(r)["age"] > 20]))
    print("  against, <= 20 min, size under 6 / 6 or more: " + line([r for r in rows if sh(r) and sh(r)["dir"] < 0 and sh(r)["age"] <= 20 and sh(r)["size"] < 6]) + " || " + line([r for r in rows if sh(r) and sh(r)["dir"] < 0 and sh(r)["age"] <= 20 and sh(r)["size"] >= 6]))
    print("  against, <= 20 min, held (>= 0.5) / not    : " + line([r for r in rows if sh(r) and sh(r)["dir"] < 0 and sh(r)["age"] <= 20 and sh(r)["held"] >= 0.5]) + " || " + line([r for r in rows if sh(r) and sh(r)["dir"] < 0 and sh(r)["age"] <= 20 and sh(r)["held"] < 0.5]))
