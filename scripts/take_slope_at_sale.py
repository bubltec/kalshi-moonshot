"""At each real live profit take: would the slope test have called the bid still rising, and what did holding pay? (2026-11-14, after SOL 21:15 YES sold at 0.982 with the bid still climbing.)
For every crypto_multiscale profit take since the take went live, at the second it sold: the bid's rise over the look back (curve_take_slope_share x time left, the live 0.08), the market's noise for that look back (the typical minute the sale itself logged, x sqrt(look / 60)), and the bid's distance under its look-back high. Then the rise bar as a share of the noise: 1.0 is the live test (rise > noise), lower is a smaller bar, 0.0 is any rise at all, with the bid still within the noise of its high. Same bets: against holding to settlement, fee on every leg, the result of the bets each variant would have held at that second (a held bet might still have sold a moment later; that is not walked). Settings chosen after seeing the results are said to be so.
Run on the host: python scripts/take_slope_at_sale.py"""
import bisect
import datetime
import re

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute(
    "SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE ? ORDER BY exit_ts",
    (SINCE, "curve risk exit (profit take)%")).fetchall()
BARS = (1.0, 0.5, 0.25, 0.0)
tot = {b: [0, 0.0, 0.0, 0, 0] for b in BARS}  # held at that second, sale result, hold result, hold beat sale, sale beat hold
print("take                       sold    left  look  rise   noise  high-bid  | would call it still rising at bar 1.0 / 0.5 / 0.25 / 0.0 | hold result minus sale result")
for r in rows:
    t = float(r["exit_ts"])
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    left = close - t
    look = 0.08 * left
    m = re.search(r"typical minute ([0-9.]+)", r["exit_reason"] or "")
    unit = float(m.group(1)) if m else None
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - look - 5.0, t)).fetchall()
    side = r["side"]
    ts = [float(x[0]) for x in q]
    bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    if len(ts) < 3 or unit is None:
        continue
    def at(x):
        i = bisect.bisect_right(ts, x) - 1
        return bd[i] if i >= 0 else bd[0]
    grid = [at(t - look + k) for k in range(int(look) + 1)]
    bid, high, start = grid[-1], max(grid), grid[0]
    noise = unit * (look / 60.0) ** 0.5
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    sale = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    hold = None if r["outcome"] not in ("yes", "no") else (n if r["outcome"] == side else 0.0) - cost
    calls = []
    for b in BARS:
        rising = (bid - start > b * noise) and (high - bid < noise)
        calls.append(rising)
        if rising and hold is not None:
            tot[b][0] += 1; tot[b][1] += sale; tot[b][2] += hold
            tot[b][3] += hold > sale; tot[b][4] += sale > hold
    print(f"{r['ticker'][2:-3]:22s} {datetime.datetime.fromtimestamp(t, ET):%H:%M:%S} {left:5.0f} {look:5.0f}  {bid-start:+.3f} {noise:.3f}  {high-bid:.3f}   | {' / '.join('YES' if c else 'no ' for c in calls)} | {'' if hold is None else f'{hold - sale:+.2f}'}")
print()
for b in BARS:
    k = tot[b]
    print(f"rise bar {b:.2f} x noise: would hold {k[0]} of the settled takes at the second they sold; those sales made {k[1]:+.2f}, holding to settlement {k[2]:+.2f} (holding minus sale {k[2]-k[1]:+.2f}); holding beat the sale on {k[3]}, the sale beat holding on {k[4]}")
print("DONE")
