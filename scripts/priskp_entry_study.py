"""PriskP entry study (2026-11-22; user: the bot buys too soon; notes for "I didn't buy here"; refine the entry on the fresh run).
QUESTION: on the same markets, does buying LATER than the model's first pass, or only after the checks have held, do better? Held to settlement, a take at the side's ask, fee on both legs (0.07 p (1-p)), $10 a buy, one buy a market, never an ask 0.45-0.55 (the user's own), minutes 2-14 (the user's). Every rule below is stated before the run, from the user's feedback; none is tuned.
THE MODEL'S PASS (as scripts/priskp_run.py): at a 10-second reading a side passes when all five checks (cushion, trend, history, price trend, price dip) are read and in their own-history fifths 4-5. RULES:
  E0 first pass (the model as run).
  E1 CONFIRM-3: the same side passes at 3 readings in a row (30 s), buy at the third.
  E2 CONFIRM-6: the same side passes at 6 readings in a row (60 s), buy at the sixth.
  E3 FROM MINUTE 4: first pass from minute 4 (the user: the biggest losses are in the first 4 minutes, not when he would bet to win settlement).
  E4 STRONG CUSHION: first pass with the cushion in fifth 5 (not 4-5): the thin-cushion lead of the notes draft.
  E5 STRONG CUSHION AND CONFIRM-3.
  E6 SECOND SIDE: a side that was refused at some reading and is later the first to pass after the OTHER side passed first and the market has moved against it (the notes draft's "didn't buy here, the other direction may be good later"): the first pass of the side opposite to the side E0 bought, after that reading. Reported as its own group on the markets where E0 bought, and as a rebuy on the same market (held to settlement).
Reported: buys, net per $1, share ahead against price paid plus fee (BEAT or no), by buy-minute band and price band; and the markets with no buy under the rule. Input: priskp_decisions.json.gz (scripts/priskp_run.py). Run: python3 scripts/priskp_entry_study.py priskp_decisions.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
D = json.load(gzip.open(sys.argv[1]))
DEAD = (0.45, 0.55)
ok = lambda cs: all(c in "45" for c in cs)


def passes(rd):
    """(yes_pass, no_pass) at one reading, with the price band."""
    s, ya, na, cy, cn = rd[:5]
    return (ok(cy) and 0.01 <= ya <= 0.99 and not DEAD[0] <= ya <= DEAD[1], ok(cn) and 0.01 <= na <= 0.99 and not DEAD[0] <= na <= DEAD[1])


def buy(m, rule):
    log = m["log"]; P = [passes(r) for r in log]
    runlen = {"yes": 0, "no": 0}
    for i, r in enumerate(log):
        for k, side in enumerate(("yes", "no")):
            runlen[side] = runlen[side] + 1 if P[i][k] else 0
        for k, side in enumerate(("yes", "no")):
            if not P[i][k]: continue
            c = (r[3] if side == "yes" else r[4])
            if rule == "E0" or (rule == "E3" and r[0] >= 240) or (rule == "E4" and c[0] == "5"):
                return side, r[0], r[1 + k]
            if rule == "E1" and runlen[side] >= 3: return side, r[0], r[1 + k]
            if rule == "E2" and runlen[side] >= 6: return side, r[0], r[1 + k]
            if rule == "E5" and c[0] == "5" and runlen[side] >= 3: return side, r[0], r[1 + k]
    return None


def second(m):
    """E6: after the first pass (E0), the first later reading where the OTHER side passes."""
    b = buy(m, "E0")
    if not b: return None
    other = "no" if b[0] == "yes" else "yes"
    for r in m["log"]:
        if r[0] <= b[1]: continue
        p = passes(r)
        if p[1 if other == "no" else 0]: return other, r[0], r[2 if other == "no" else 1]
    return None


def row(m, b):
    side, s, price = b
    won = m["won_yes"] if side == "yes" else not m["won_yes"]
    return {"minute": s / 60.0, "price": price, "won": won, "net": ((1.0 if won else 0.0) - price - fee(price)) * 10.0 / price}


def line(g, total=None):
    n = len(g)
    if n < 30: return "n %4d (too few)" % n
    w = sum(r["won"] for r in g) / n; nd = sum(r["price"] + fee(r["price"]) for r in g) / n
    return "n %5d%s  net %+7.0f$ %+5.1fc per $1  ahead %2.0f%% need %2.0f%% paid %.3f %s" % (n, "" if total is None else f" of {total}", sum(r["net"] for r in g), 100 * sum(r["net"] for r in g) / (10.0 * n), 100 * w, 100 * nd, sum(r["price"] for r in g) / n, "BEAT" if w > nd else "no")


R = {}
for rule in ("E0", "E1", "E2", "E3", "E4", "E5"):
    R[rule] = [row(m, b) for m in D if (b := buy(m, rule))]
R["E6"] = [row(m, b) for m in D if (b := second(m))]
print(f"{len(D)} markets with a decision log\n")
names = {"E0": "first pass (the model as run)", "E1": "confirm 3 readings (30 s)", "E2": "confirm 6 readings (60 s)", "E3": "first pass from minute 4", "E4": "strong cushion (fifth 5)", "E5": "strong cushion and confirm 3", "E6": "the other side's first later pass (a second buy on the same market)"}
for k in R: print(f"{k} {names[k]:62s} " + line(R[k], len(D)))
print("\nBY BUY MINUTE")
for k in R:
    print(f"  {k}: " + " | ".join(f"{a}-{int(b)}: " + (line([r for r in R[k] if a <= r['minute'] < b]).replace("  net", "").split("per $1")[0].strip() if len([r for r in R[k] if a <= r['minute'] < b]) >= 30 else "few") for a, b in ((2, 4), (4, 7), (7, 10), (10, 12), (12, 14.01))))
print("\nBY PRICE PAID")
for k in R:
    print(f"  {k}: " + " | ".join(f"{a:.2f}-{b:.2f}: " + (f"{100 * sum(r['net'] for r in g) / (10.0 * len(g)):+.1f}c n{len(g)}" if len(g) >= 30 else "few") for a, b in ((0.55, 0.65), (0.65, 0.85), (0.85, 1.0)) for g in [[r for r in R[k] if a <= r['price'] < b]]))
