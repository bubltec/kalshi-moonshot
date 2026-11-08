"""Does the shock exit need to see volume? (2026-11-08, user: "the shock exit needs to look at volume as well"; SOL 12:30 YES sold at 0.55 by the shock exit with 223 s left and won, "a little dip ... wasn't a volume sweep").
Question: on the live crypto shock-exit sales since 11-02, do the sales that came with a burst of taker volume against the holder beat holding to the result, and the ones without one lose to it? The flow is the tape's flow_1s (contracts bought by takers on each side per second, per market). For a YES holder the adverse side is NO takers (they push YES down), for a NO holder YES takers.
Reading, as watcher._side_burst: the adverse contracts in the last W seconds before the sale over W times the market's own normal adverse second (the median second of the 120 s before that window, plus 0.01). Windows W = 10 and 30 s. Also the adverse contracts in W seconds as a raw count, and the share of all taker contracts in W seconds that were adverse. Bands are a grid fixed before the results.
Setup: decisions of crypto lanes whose exit_reason starts with 'shock exit', their contracts, side, exit price and time, the window's result. Change = sale proceeds (price less the sell fee, times contracts) - what the position pays at the result. Reported per band: sales, net change in dollars, mean per sale with its standard error, beat hold / lost, the two halves each with its standard error; and the SOL 12:30 sale's own reading.
Not covered: what a flow requirement would have done to the later path of the vetoed sales, the sale slippage, other exits.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_flow_study.py
"""

import calendar
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.exit_ts,d.exit_price,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane LIKE 'crypto%' AND d.exit_ts>? AND d.exit_reason LIKE 'shock exit%' AND d.exit_price>0 AND d.contracts>0 AND w.result IN ('yes','no') ORDER BY d.exit_ts",
    (t0,),
).fetchall()
print("shock-exit sales of crypto lanes with a settled window:", len(rows))
out = []
for r in rows:
    t = int(r["exit_ts"])
    fl = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (r["ticker"], t - 200, t)).fetchall()
    book = {int(x[0]): (float(x[1]), float(x[2])) for x in fl}
    idx = 1 if r["side"] == "yes" else 0  # the adverse side: NO takers for a YES holder
    rd = {}
    for W in (10, 30):
        adv = sum(book.get(t - k, (0.0, 0.0))[idx] for k in range(W))
        fav = sum(book.get(t - k, (0.0, 0.0))[1 - idx] for k in range(W))
        per = sorted(book.get(t - W - k, (0.0, 0.0))[idx] for k in range(120))
        base = (per[59] + per[60]) / 2.0
        rd[W] = (adv / (W * base + 0.01), adv, adv / (adv + fav) if adv + fav > 0 else 0.0)
    n = float(r["contracts"])
    bid = float(r["exit_price"])
    hold = n if r["result"] == r["side"] else 0.0
    out.append((float(r["exit_ts"]), rd, n * (bid - fee(bid)) - hold, r["ticker"], len(fl)))
print("read:", len(out), "| with no flow rows at all:", sum(1 for o in out if o[4] == 0))
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
        print(f"   {label:36s} n=0")
        return
    ch = [o[2] for o in xs]
    a = [o[2] for o in xs if o[0] <= mid]
    b = [o[2] for o in xs if o[0] > mid]
    print(f"   {label:36s} net ${sum(ch):+8.2f} | per sale {ms(ch)} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | first half {ms(a)} | second half {ms(b)}")


line("all shock sales", out)
for W in (10, 30):
    print(f"\nadverse taker volume in the last {W} s, as a multiple of the market's own normal second:")
    for lo, hi in ((0, 1.5), (1.5, 3), (3, 6), (6, 1e9)):
        line(f"ratio {lo:g} to {hi:g}", [o for o in out if lo <= o[1][W][0] < hi])
    print(f"adverse contracts in the last {W} s (raw):")
    for lo, hi in ((0, 20), (20, 100), (100, 400), (400, 1e9)):
        line(f"{lo:g} to {hi:g} contracts", [o for o in out if lo <= o[1][W][1] < hi])
print("\nthe SOL 12:30 sale:")
for o in out:
    if o[3].startswith("KXSOL15M-26NOV081230"):
        print("   ", {W: (round(o[1][W][0], 2), round(o[1][W][1], 1), round(o[1][W][2], 2)) for W in (10, 30)}, "(ratio, adverse contracts, adverse share); change", round(o[2], 2))
print("DONE")
