"""Do crypto coins move together in the shock moments, and does it matter for the shock exit? (2026-11-08, user: "i think the crypto markets tend to move in those shock moments together. right?").
Question A: at the moment of each live shock-exit sale on a crypto bet, how many of the nine coins (the settlement indexes) had moved against the bet's direction by a typical minute or more over the last 60 s? (Adverse for a YES bet is down, for a NO bet up; each coin read in its own units: its log move over 60 s over its own root-mean-square one-second move of the prior 900 s times sqrt(60).) And how does that compare with a random second (the base rate of a coin moving a typical minute in 60 s, and the number of the nine that do at random)?
Question B: the sale against holding to the result, by that count (the breadth): 1 (only the coin itself), 2 to 3, 4 to 6, 7 and over. Is a market-wide shock more or less likely to be the real thing?
Setup: the live crypto shock-exit sales since 11-02 (decisions with exit_reason starting 'shock exit'), the window's result, change = sale proceeds (exit price less the sell fee, times contracts) - what the position pays at the result. Bands are a grid fixed before the results. The coin's own count includes itself.
Not covered: the later path of a vetoed sale, slippage.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_breadth_study.py
"""

import bisect
import calendar
import math
import random

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")
IDS = {x: variants.crypto_index(x) for x in SERIES}
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.exit_ts,d.exit_price,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane LIKE 'crypto%' AND d.exit_ts>? AND d.exit_reason LIKE 'shock exit%' AND d.exit_price>0 AND d.contracts>0 AND w.result IN ('yes','no') ORDER BY d.exit_ts",
    (t0,),
).fetchall()
print("shock-exit sales:", len(rows))


def ticks(ix, t):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t - 1000, t)).fetchall()
    return [float(x[0]) for x in r], [float(x[1]) for x in r]


def at(ts, vs, t):
    i = bisect.bisect_right(ts, t) - 1
    return vs[i] if i >= 0 else None


def move60(ts, vs, t):
    """The coin's log move over 60 s ending at t, in its own typical minutes (signed, up positive), or None."""
    a, b = at(ts, vs, t), at(ts, vs, t - 60)
    if a is None or b is None or len(ts) < 300:
        return None
    steps, last = [], None
    for k in range(int(t) - 900, int(t)):
        v = at(ts, vs, k)
        if v is not None and last is not None:
            steps.append(math.log(v / last))
        last = v
    if len(steps) < 300:
        return None
    rate = math.sqrt(sum(x * x for x in steps) / len(steps))
    return math.log(a / b) / (rate * math.sqrt(60.0)) if rate > 0 else None


def breadth_at(t, sign):
    n, seen = 0, 0
    for ix in IDS.values():
        if not ix:
            continue
        ts, vs = ticks(ix, t)
        m = move60(ts, vs, t)
        if m is None:
            continue
        seen += 1
        if -m * sign >= 1.0:
            n += 1
    return n, seen


out = []
for r in rows:
    t = float(r["exit_ts"])
    sign = 1.0 if r["side"] == "yes" else -1.0
    b, seen = breadth_at(t, sign)
    n = float(r["contracts"])
    bid = float(r["exit_price"])
    hold = n if r["result"] == r["side"] else 0.0
    out.append((t, b, seen, n * (bid - fee(bid)) - hold, r["ticker"]))
print("read:", len(out))
random.seed(1)
base = []
span = [x[0] for x in out]
for _ in range(60):
    t = random.uniform(min(span), max(span))
    b, seen = breadth_at(t, random.choice((1.0, -1.0)))
    if seen:
        base.append(b)
print("mean number of the nine coins moving a typical minute against a bet's direction in 60 s: at the shock sales", round(sum(o[1] for o in out) / len(out), 2), "| at random seconds", round(sum(base) / max(len(base), 1), 2), f"(n={len(base)})")
print("share of random seconds with 4 or more of the nine:", round(sum(1 for b in base if b >= 4) / max(len(base), 1), 3), "| share of shock sales with 4 or more:", round(sum(1 for o in out if o[1] >= 4) / len(out), 3))
mid = sorted(o[0] for o in out)[len(out) // 2]


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


def line(label, xs):
    if not xs:
        print(f"   {label:26s} n=0")
        return
    ch = [o[3] for o in xs]
    a = [o[3] for o in xs if o[0] <= mid]
    b = [o[3] for o in xs if o[0] > mid]
    print(f"   {label:26s} net ${sum(ch):+8.2f} | per sale {ms(ch)} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | first half {ms(a)} | second half {ms(b)}")


print("\nshock sales against holding, by the number of the nine coins moving against the bet's direction:")
line("all", out)
for lo, hi in ((0, 2), (2, 4), (4, 7), (7, 10)):
    line(f"{lo}-{hi - 1} coins" if hi - lo > 1 else f"{lo} coin", [o for o in out if lo <= o[1] < hi])
print("\nthe SOL 12:30 sale:")
for o in out:
    if o[4].startswith("KXSOL15M-26NOV081230"):
        print("   coins moving against the bet:", o[1], "of", o[2], "| change", round(o[3], 2))
print("DONE")
