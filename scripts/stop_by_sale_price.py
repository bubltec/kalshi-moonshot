"""Max-loss sales by the price they sold at: does the stop sell bets the market still rates as likely? (2026-11-16, user, on sales and buys near 50 cents: "how does our math figure its a good time to sell or buy at 50c?"; "there is definitely an issue with some calculation")

The stop's rule (curverisk.stopped): sell when the loss, as a share of cost, reaches max_loss (0.5) times the chance the side pays, and that chance is the bid net of its fee. Solved for the bid, a bet with an all-in cost of c is sold once the net bid is c / (1 + 0.5 c) or under: bought at 0.60 it sells at about 0.49, at 0.70 at 0.54, at 0.80 at 0.59, at 0.90 at 0.64. So the dearer the bet, the higher the price it is sold at, and a bet bought over about 0.62 is sold while the market still rates it better than even.
One question on the same bets (CLAUDE.md rule 4), every live lane, from the decisions and windows tables only: each max-loss sale since 2026-11-14 17:02 ET against holding that bet to settlement, fee on every leg, by the price it sold at. Bands fixed before the run.

Run on the host: scripts/cloud.sh run scripts/stop_by_sale_price.py
"""
import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = []
for d in st.execute("SELECT ts, lane, ticker, side, ask, stake_usd, exit_price, exit_ts FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE '%(max loss)%' ORDER BY ts", (SINCE,)).fetchall():
    if d["ticker"] not in res or d["exit_price"] is None or not d["ask"]:
        continue
    result, close = res[d["ticker"]]
    a, px = float(d["ask"]), float(d["exit_price"])
    basis = a + fee(a)
    n = float(d["stake_usd"]) / basis
    won = result == d["side"]
    rows.append({"lane": "crypto" if d["lane"].startswith("crypto") else "commodity", "px": px, "ask": a, "won": won, "diff": ((px - fee(px)) - (1.0 if won else 0.0)) * n, "held_s": float(d["exit_ts"]) - float(d["ts"]), "left": close - float(d["exit_ts"])})


def line(name, rs):
    if len(rs) < 3:
        print(f"  {name:36s} n {len(rs):3d}")
        return
    n = len(rs)
    print(f"  {name:36s} n {n:3d} | bought at {sum(r['ask'] for r in rs) / n:.2f}, sold at {sum(r['px'] for r in rs) / n:.2f} | would have won held {sum(1 for r in rs if r['won']):3d} ({sum(1 for r in rs if r['won']) / n:4.0%}) | sale less hold ${sum(r['diff'] for r in rs):+8.2f}")


print(f"window: since 2026-11-14 17:02 ET; {len(rows)} live max-loss sales on every lane")
for lane in ("crypto", "commodity"):
    sub = [r for r in rows if r["lane"] == lane]
    print(f"\n{lane} lanes")
    line("all", sub)
    for lo, hi, name in ((0.55, 1.0, "sold at 0.55 and over"), (0.45, 0.55, "sold at 0.45 to 0.55"), (0.30, 0.45, "sold at 0.30 to 0.45"), (0.0, 0.30, "sold under 0.30")):
        line(name, [r for r in sub if lo <= r["px"] < hi])
    line("sold within 2 min of the buy", [r for r in sub if r["held_s"] < 120])
print("DONE")
