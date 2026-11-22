"""PriskP exit study: the user's giveback exit (2026-11-22, the user's own specification, taken as written).
DEFINITIONS (the user's): T = seconds left to the close; u = the bet's own typical minute = the root-mean-square one-second change of the bet's bid over the last 15 minutes times sqrt(60) (the log is every 10 s, so u = the rms 10-second change times sqrt(6), over the bids in the log up to now, from minute 2 on, a window of at most 12 minutes, read from 7 changes on); peak = the highest bid since the buy; fees = the entry fee plus the sell fee per contract (0.07 p (1-p) each).
THE RULE (the user's, as clarified in his second message): giveback(T) = k x u x sqrt(T / 60), read at every reading from the spot's CURRENT side of the target. WINNING (the spot is on the bet's side of the strike, the bet's cushion above zero): a LARGE k: hold until the price starts to fade; sell when bid <= peak - giveback(T), only if bid - entry > fees (never at a loss), and the allowed drop closes in as T falls. UNDER THE TARGET (the spot is not on the bet's side): aim to exit at the maximum value when the price increases: once the bid has risen above its level at the buy (peak above the buy-time bid by a cent or more), sell at the first reading where bid <= peak - giveback(T) with a VERY SMALL k; this may be a profit, a break-even or a small loss, so there is no profit condition in this state. Nothing is sold before the bid has risen in the under-the-target state. THE NUMBERS ARE MINE AND NAMED because the user gave none: large k = 2.0, very small k = 0.1; sensitivity (not chosen): large 1.0 and 3.0, small 0.05 and 0.25. The bid at a reading is 1 minus the other side's ask; the log ends at minute 14, so nothing is sold in the last minute (held from there). A sale is at the bid of that reading; not covered: whether it would fill.
THE ENTRIES (stated in scripts/priskp_entry_study.py): E0 first pass, E2 six readings in a row, E5 cushion in fifth 5 and three readings in a row, E7 cushion in fifth 5 and six in a row; a take at the ask, minutes 2-14, never an ask 0.45-0.55, one buy a market, held-to-settlement is the comparison.
Reported per entry: held, the giveback exit (stated k), the sensitivities; split by whether the spot had crossed to the bet's side at the buy; sold share; by buy minute. Input: priskp_decisions.json.gz with the cushion in each log entry. Run: python3 scripts/priskp_exit_giveback_study.py priskp_decisions.json.gz"""
import gzip, json, math, sys
fee = lambda p: 0.07 * p * (1.0 - p)
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)
KS = {"stated": (2.0, 0.1), "L1": (1.0, 0.1), "L3": (3.0, 0.1), "S05": (2.0, 0.05), "S25": (2.0, 0.25)}


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


def trade(m, i, side, kl, ks):
    log = m["log"]
    entry = log[i][1] if side == "yes" else log[i][2]
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    fe = fee(entry)
    hold = ((1.0 if won else 0.0) - entry - fe) * 10.0 / entry
    sg = 1.0 if side == "yes" else -1.0
    bids = [1.0 - (r[2] if side == "yes" else r[1]) for r in log]
    at_buy = sg * log[i][5] > 0
    bid0 = bids[i]
    peak = 0.0
    for j in range(i + 1, len(log)):
        crossed = sg * log[j][5] > 0  # the spot's CURRENT side of the target
        b = bids[j]; peak = max(peak, b)
        T = 900.0 - log[j][0]
        if T <= 60.0 or j < 7: continue  # the log ends at minute 14: not sold in the last minute
        d = [y - x for x, y in zip(bids[:j], bids[1:j + 1])]
        if len(d) < 7: continue
        u = math.sqrt(sum(v * v for v in d[-90:]) / len(d[-90:])) * math.sqrt(6.0)  # at most the last 15 minutes of 10-second changes
        k = kl if crossed else ks
        profit_ok = (b - entry > fe + fee(b)) if crossed else (peak >= bid0 + 0.01)  # winning: never at a loss; under the target: only after the price has risen, any result
        if u > 0 and b <= peak - k * u * math.sqrt(T / 60.0) and profit_ok:
            return ((b - fee(b)) - entry - fe) * 10.0 / entry, hold, True, at_buy
    return hold, hold, False, at_buy


def fmt(g, ix):
    n = len(g)
    if n < 30: return "n %5d (too few)" % n
    return "n %5d  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%%  sold %2.0f%%" % (n, sum(x[ix][0] for x in g), 100 * sum(x[ix][0] for x in g) / (10.0 * n), 100 * sum(1 for x in g if x[ix][0] > 0) / n, 100 * sum(1 for x in g if x[ix][2]) / n)


for rule in ("E0", "E2", "E5", "E7"):
    rows = []
    for m in D:
        b = pick(m, rule)
        if not b: continue
        i, side = b
        res = {k: trade(m, i, side, *v) for k, v in KS.items()}
        rows.append({"held": (res["stated"][1], res["stated"][1], False), **{k: v[:3] for k, v in res.items()}, "at_buy": res["stated"][3], "minute": m["log"][i][0] / 60.0})
    print(f"\n{rule}: buys on {len(rows)} of {len(D)} markets")
    print("  held             : " + fmt(rows, "held"))
    print("  giveback (k 2.0 / 0.1): " + fmt(rows, "stated"))
    for k, lab in (("L1", "large k 1.0"), ("L3", "large k 3.0"), ("S05", "small k 0.05"), ("S25", "small k 0.25")): print(f"    sensitivity, not chosen, {lab}: " + fmt(rows, k))
    for lab, g in (("spot already on the bet's side at the buy", [r for r in rows if r["at_buy"]]), ("spot not on the bet's side at the buy", [r for r in rows if not r["at_buy"]])):
        print(f"  {lab}: held " + fmt(g, "held") + "\n      giveback " + fmt(g, "stated"))
    print("  by buy minute (held | giveback): " + " | ".join(f"{a}-{int(b)}: " + (f"{100 * sum(r['held'][0] for r in g) / (10.0 * len(g)):+.1f}c | {100 * sum(r['stated'][0] for r in g) / (10.0 * len(g)):+.1f}c n{len(g)}" if len(g) >= 30 else "few") for a, b in ((2, 4), (4, 7), (7, 10), (10, 12), (12, 14.01)) for g in [[r for r in rows if a <= r["minute"] < b]]))
