"""Should the shock exit stand down when the curve's own chance says hold? (2026-11-08, user: SOL 12:30 YES bought at 0.83 was sold at 0.55 by the shock exit with 223 s left and the cushion still +2.0 bp, and won: "a little dip but nothing worth selling over ... how could we avoid this in the future?").
At the sale the value watch had logged: bid 0.70, curve chance 0.73, over by -0.042, i.e. the confidence model valued holding above the bid, and the shock exit sold anyway.
Question: on the live crypto shock-exit sales since 11-02, how does the sale compare with holding to the result, split by how far the sale's bid (after the sell fee) sat over the confidence model's chance that the side pays (over = bid - fee - chance; the value exit's own measure)? If sales with a negative over lose to holding, a shock sale that the value check says to hold is a false positive.
Setup: decisions of crypto lanes whose exit_reason starts with 'shock exit', with their contracts, side, exit price and time, the strike and close from windows. The chance is confidence.py's model (confidence_model.json as it is now, fitted on days including these: partly in-sample, said here) read from the tape's index ticks at the sale time, the quote taken as the sale price (a sale that slipped under its trigger bid, as SOL 12:26 did, 0.69 to 0.55, reads lower here). Change = sale proceeds (price less the sell fee, times contracts) - what the position pays at the result.
Reported per band of over: sales, net change in dollars with the mean per sale and its standard error, beat hold / lost, and the two halves each with its standard error.
Not covered: what the same sales would have done under a rule that vetoes them (the veto changes the later path), the exit orders' slippage, other exits.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_vs_value_study.py
"""

import calendar
import math

from kalshi_moonshot import confidence, config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
model = confidence.load_model(s)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.exit_ts,d.exit_price,d.exit_reason,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane LIKE 'crypto%' AND d.exit_ts>? AND d.exit_reason LIKE 'shock exit%' AND d.exit_price>0 AND d.contracts>0 AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.exit_ts",
    (t0,),
).fetchall()
print("shock-exit sales of crypto lanes with a settled window:", len(rows), "| confidence model:", "loaded" if model else "MISSING")
out = []
for r in rows:
    ser = r["ticker"].split("-")[0]
    ix = variants.crypto_index(ser)
    if not ix or not model:
        continue
    t = float(r["exit_ts"])
    ticks = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, t - 3760.0, t)).fetchall()
    if len(ticks) < 100:
        continue
    T = [float(x[0]) for x in ticks]
    V = [float(x[1]) for x in ticks]
    bid = float(r["exit_price"])
    ask = min(0.99, bid + 0.01)
    yes_bid, yes_ask = (bid, ask) if r["side"] == "yes" else (max(0.01, 1.0 - ask), 1.0 - bid)
    f = confidence.features_at(T, V, t, float(r["strike"]), float(r["close_ts"]) - t, yes_bid, yes_ask)
    if f is None:
        continue
    p_yes = confidence.predict(model["w"], confidence.vector(f, model["mu"], model["sd"]))
    chance = p_yes if r["side"] == "yes" else 1.0 - p_yes
    n = float(r["contracts"])
    hold = n if r["result"] == r["side"] else 0.0
    out.append((t, bid - fee(bid) - chance, n * (bid - fee(bid)) - hold, r["lane"], float(r["close_ts"]) - t))
print("read:", len(out))
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
        print(f"   {label:34s} n=0")
        return
    ch = [o[2] for o in xs]
    a = [o[2] for o in xs if o[0] <= mid]
    b = [o[2] for o in xs if o[0] > mid]
    print(f"   {label:34s} net ${sum(ch):+8.2f} | per sale {ms(ch)} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | first half {ms(a)} | second half {ms(b)}")


print("\nshock-exit sales vs holding, by over = bid - fee - the curve's chance:")
line("all", out)
for lo, hi in ((-9, -0.05), (-0.05, -0.02), (-0.02, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 9)):
    line(f"over {lo:+.2f} to {hi:+.2f}", [o for o in out if lo <= o[1] < hi])
print("\nthe sales a veto at over below 0 would have held (curve says hold):")
line("over < 0", [o for o in out if o[1] < 0])
line("over < -0.02", [o for o in out if o[1] < -0.02])
print("\nby time left at the sale:")
for lo, hi in ((0, 120), (120, 240), (240, 301)):
    line(f"{lo}-{hi} s left, over < 0", [o for o in out if o[1] < 0 and lo <= o[4] < hi])
print("DONE")
