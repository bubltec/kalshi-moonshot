"""Does Hyperliquid's perp tell a spike from a dip at the shock exit? (2026-11-08, user: "the hyperliquid read ahead ... just so we can see if it's a spike or not").
Question: when the shock exit fires on a crypto bet, was the perpetual future (Hyperliquid, tape fut_1s: the last trade price each second) also moving against the bet? Sales where the perp confirms (it moved against the bet by a typical move or more over the last h seconds) against sales where it does not: how does each compare with holding to the result?
Coverage: fut_1s records BTC, ETH, SOL, XRP and DOGE only (FUT_FLOW_COINS), so the other coins' shock sales are left out and counted.
Reading: for a sale at time t, the perp's log move against the bet over h seconds (h = 10, 30, 60) in that perp's own units: the move divided by (the root-mean-square one-second log move of the perp over the 900 s before t) times sqrt(h). Positive = against the bet. Bands are a grid fixed before the results: under 0.5, 0.5 to 1, 1 to 2, 2 and over. The index's own move in the same units is printed beside it as a check.
Setup: the live crypto shock-exit sales since 11-02 (decisions with exit_reason starting 'shock exit', their contracts, side, exit price and time, the window's result). Change = sale proceeds (exit price less the sell fee, times contracts) - what the position pays at the result.
Reported per band: sales, net change in dollars, mean per sale with its standard error, beat hold / lost, the two halves each with theirs; and the SOL 12:30 sale's reading.
Not covered: what the later path of a vetoed sale would have been, slippage, the other coins.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_hl_confirm_study.py
"""

import bisect
import calendar
import math

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
COVER = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.exit_ts,d.exit_price,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane LIKE 'crypto%' AND d.exit_ts>? AND d.exit_reason LIKE 'shock exit%' AND d.exit_price>0 AND d.contracts>0 AND w.result IN ('yes','no') ORDER BY d.exit_ts",
    (t0,),
).fetchall()
covered = [r for r in rows if r["ticker"].split("-")[0] in COVER]
print("shock-exit sales:", len(rows), "| on the five coins the perp feed covers:", len(covered))


def grid(sym, t):
    r = tape.execute("SELECT ts,px FROM fut_1s WHERE symbol=? AND ts BETWEEN ? AND ? AND px>0 ORDER BY ts", (sym, int(t) - 1000, int(t))).fetchall()
    ts = [int(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    return ts, vs


def idx_grid(ix, t):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t - 1000, t)).fetchall()
    return [float(x[0]) for x in r], [float(x[1]) for x in r]


def at(ts, vs, t):
    i = bisect.bisect_right(ts, t) - 1
    return vs[i] if i >= 0 else None


def adverse(ts, vs, t, h, sign):
    a, b = at(ts, vs, t), at(ts, vs, t - h)
    if a is None or b is None or len(ts) < 200:
        return None
    steps = []
    last = None
    for k in range(int(t) - 900, int(t)):
        v = at(ts, vs, k)
        if v is not None and last is not None:
            steps.append(math.log(v / last))
        last = v
    if len(steps) < 300:
        return None
    rate = math.sqrt(sum(x * x for x in steps) / len(steps))
    if rate <= 0:
        return None
    return -math.log(a / b) * sign / (rate * math.sqrt(h))


out = []
for r in covered:
    t = float(r["exit_ts"])
    sym = COVER[r["ticker"].split("-")[0]]
    ts, vs = grid(sym, t)
    its, ivs = idx_grid(variants.crypto_index(r["ticker"].split("-")[0]), t)
    sign = 1.0 if r["side"] == "yes" else -1.0
    rd = {h: adverse(ts, vs, int(t), h, sign) for h in (10, 30, 60)}
    ri = {h: adverse(its, ivs, t, h, sign) for h in (10, 30, 60)}
    if any(v is None for v in rd.values()):
        continue
    n = float(r["contracts"])
    bid = float(r["exit_price"])
    hold = n if r["result"] == r["side"] else 0.0
    out.append((t, rd, ri, n * (bid - fee(bid)) - hold, r["ticker"]))
print("read (perp rows present):", len(out))
mid = sorted(o[0] for o in out)[len(out) // 2] if out else 0


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


def line(label, xs):
    if not xs:
        print(f"   {label:30s} n=0")
        return
    ch = [o[3] for o in xs]
    a = [o[3] for o in xs if o[0] <= mid]
    b = [o[3] for o in xs if o[0] > mid]
    print(f"   {label:30s} net ${sum(ch):+7.2f} | per sale {ms(ch)} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | first half {ms(a)} | second half {ms(b)}")


line("all covered shock sales", out)
for h in (10, 30, 60):
    print(f"\nthe perp's move against the bet over {h} s, in its own units:")
    for lo, hi in ((-99, 0.5), (0.5, 1), (1, 2), (2, 99)):
        line(f"perp {lo:g} to {hi:g}", [o for o in out if lo <= o[1][h] < hi])
    print(f"   mean perp / index in the same units: {sum(o[1][h] for o in out) / len(out):+.2f} / {sum(o[2][h] for o in out if o[2][h] is not None) / max(1, sum(1 for o in out if o[2][h] is not None)):+.2f}")
print("\nthe SOL 12:30 sale (perp, index; units against the bet):")
for o in out:
    if o[4].startswith("KXSOL15M-26NOV081230"):
        print("   ", {h: (round(o[1][h], 2), round(o[2][h], 2) if o[2][h] is not None else None) for h in (10, 30, 60)}, "change", round(o[3], 2))
print("DONE")
