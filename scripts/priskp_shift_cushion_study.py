"""PriskP shift-out-of-cushion study (2026-11-22; user: "no buys on the side of the shift. So if yes side shifts down then don't buy yes if it shifted out of cushion (during current target, make sure everything is always evaluated against current target otherwise everything is skewed to each 15 mins)").
THE RULE TESTED (the user's): a side is blocked when a confirmed shift took the spot OUT of that side's cushion, read against THIS market's target: the level before the shift was on the side's winning side of the strike and the level after is not (a shift down blocks YES, a shift up blocks NO; a shift that stays inside the cushion blocks nothing). The levels are logged against the current market's strike (in the spot's typical minutes, YES view) by scripts/priskp_run.py. The shift is a priskp.shifts step held about 3 minutes; the run logs the confirmed shifts of the last hour every 30 s; a buy reads the latest log entry at or before it.
GROUPS (stated before the run, not tuned): BLOCKED = the LATEST confirmed shift in the hour took the buy's side out of cushion; BLOCKED_20 = the same and at most 20 minutes old; BLOCKED_ANY = any confirmed shift in the hour did; and its complement. Then BLOCKED split by whether the shift still holds (held 0.5 or more) and whether the spot is back in cushion now (the buy's cushion fifth is always 5 under E5, so the entry has already seen the spot inside). Entries (scripts/priskp_entry_study.py): E0 first pass, E5 strong cushion and 3 readings; a take at the ask, held to settlement, fee 0.07 p (1-p), $10 a buy, never an ask 0.45-0.55, minutes 2-14, one buy a market. Also the markets where E0/E5 would have bought the other side instead (the rule changes which side is bought): reported as the bet the rule would still allow, on the same markets.
Input: priskp_decisions.json.gz with the shift levels. Run: python3 scripts/priskp_shift_cushion_study.py priskp_decisions.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)
half = sorted(m["close"] for m in D)[len(D) // 2]


def passes(m, rule):
    """Yield (index, side) of every reading where a side is the rule's candidate (E0: all five pass; E5: strong cushion held three readings)."""
    log, run = m["log"], {"yes": 0, "no": 0}
    for i, r in enumerate(log):
        s, ya, na, cy, cn = r[:5]
        for side, ask, c in (("yes", ya, cy), ("no", na, cn)):
            p = ok(c) and 0.01 <= ask <= 0.99 and not DEAD[0] <= ask <= DEAD[1]
            run[side] = run[side] + 1 if p else 0
            if p and (rule == "E0" or (c[0] == "5" and run[side] >= 3)): yield i, side


def shifts_at(m, i):
    for j in range(i, -1, -1):
        r = m["log"][j]
        if len(r) > 7 and r[7] is not None:
            return [(ago + (m["log"][i][0] - r[0]), step, held, bz, az) for ago, step, held, bz, az in r[7]]
    return []


def hurt(sh, side):
    """Whether this shift took `side` out of cushion (levels in typical minutes against the current strike, YES view)."""
    ago, step, held, bz, az = sh
    sg = 1.0 if side == "yes" else -1.0
    return sg * bz > 0 and sg * az <= 0


def row(m, i, side):
    log = m["log"]; price = log[i][1] if side == "yes" else log[i][2]
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    shs = shifts_at(m, i)
    latest = shs[-1] if shs else None
    return {"price": price, "won": won, "net": ((1.0 if won else 0.0) - price - fee(price)) * 10.0 / price, "close": m["close"],
            "blocked": bool(latest and hurt(latest, side)), "blocked20": bool(latest and hurt(latest, side) and latest[0] / 60.0 <= 20), "any": any(hurt(s, side) for s in shs),
            "held": latest[2] if latest else None}


def line(g):
    n = len(g)
    if n < 30: return "n %5d (too few)" % n
    w = sum(r["won"] for r in g) / n; nd = sum(r["price"] + fee(r["price"]) for r in g) / n
    h1, h2 = [r for r in g if r["close"] < half], [r for r in g if r["close"] >= half]
    hs = "%+.1fc / %+.1fc" % tuple(100 * sum(r["net"] for r in h) / (10.0 * len(h)) if h else 0.0 for h in (h1, h2))
    return "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%% need %2.0f%% %s | halves %s" % (n, sum(r["net"] for r in g), 100 * sum(r["net"] for r in g) / (10.0 * n), 100 * w, 100 * nd, "BEAT" if w > nd else "no  ", hs)


for rule in ("E0", "E5"):
    rows, allowed = [], []
    for m in D:
        first = next(passes(m, rule), None)
        if first: rows.append(row(m, *first))
        # the same market under the user's rule: the first candidate whose side is not blocked
        for i, side in passes(m, rule):
            r = row(m, i, side)
            if not r["blocked"]:
                allowed.append(r); break
    print(f"\n{rule}: {len(rows)} buys as the entry stands; {len(allowed)} with a blocked side skipped to the next unblocked candidate")
    print("  all (as the entry stands)             : " + line(rows))
    print("  BLOCKED (latest shift took the side out of cushion): " + line([r for r in rows if r["blocked"]]))
    print("    within 20 minutes                  : " + line([r for r in rows if r["blocked20"]]))
    print("    and still held (>= 0.5)            : " + line([r for r in rows if r["blocked"] and r["held"] is not None and r["held"] >= 0.5]))
    print("    and not held                       : " + line([r for r in rows if r["blocked"] and r["held"] is not None and r["held"] < 0.5]))
    print("  BLOCKED_ANY (any shift in the hour did)  : " + line([r for r in rows if r["any"]]))
    print("  not blocked (latest shift did not)    : " + line([r for r in rows if not r["blocked"]]))
    print("  THE RULE APPLIED (blocked side skipped, next unblocked candidate taken): " + line(allowed))
