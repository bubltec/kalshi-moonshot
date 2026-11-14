"""The flip exit, replayed on the live lane's own bets (2026-11-14). ONE stated rule, before any result:

  Sell when (1) the spot has passed the strike against the bet, AND (2) the past hour's own waves say it is less likely to come back than the price we can sell at:
      share = of all the stretches of the past hour as long as the time left, the share in which the spot moved back at least as far as it is now past the strike (in the direction the bet needs);
      sell when share < the bid net of its fee, and that has held for the look back (8% of the time left).
  Hold when the spot has not crossed, however far the Kalshi price dips, and hold while the hour shows it coming back that far more often than the price says.

User: "it's more about how far off from the strike we are if we passed it and if history shows that it could (waves in the past hour)". Time left is not a gate: it only sets how long a stretch of the hour is compared.
Spot = the coin's settlement index as Kalshi's socket streams it (tape.index_ticks). Strike = the window's own (windows table). Same bets, compared with what happened (sold or settled, fee on every leg). One day; the bets are the ones the lane chose.
Run on the host: scripts/cloud.sh run scripts/flip_exit_replay.py"""
import bisect
import datetime
import math
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.commodities import instrument_for, load_instruments
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.load()
store = Store(s.db_path)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
STEP, SHARE = 5, 0.08
bets = store.db.execute(
    "SELECT id, ts, ticker, side, ask, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (float(os.environ.get("SINCE", "1794668400")),),
).fetchall()


def grid(index_id, start, end):
    got = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index_id, start - 30, end)).fetchall()
    ts, vs = [float(r[0]) for r in got], [float(r[1]) for r in got]
    out = {}
    for t in range(int(start), int(end) + 1, STEP):
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0 and t - ts[i] <= 30:
            out[t] = math.log(vs[i])
    return out


tot = {"n": 0, "sold": 0, "was": 0.0, "now": 0.0}
print("bought   coin side paid | spot at buy vs strike | crossed  | flip rule sells                          | happened        | change")
for b in bets:
    inst = instrument_for(b["ticker"].split("-")[0], load_instruments(s.data_dir))
    row = store.db.execute("SELECT strike FROM windows WHERE ticker=?", (b["ticker"],)).fetchone()
    if inst is None or not inst.symbol.startswith("kp:") or not row or not row["strike"]:
        continue
    strike, side = math.log(float(row["strike"])), b["side"]
    close = datetime.datetime.fromisoformat(b["close_time"]).timestamp()
    buy = int(math.ceil(float(b["ts"]) / STEP) * STEP)
    g = grid(inst.symbol.removeprefix("kp:"), buy - 3700, close)
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (b["ticker"], float(b["ts"]) - 5, close)).fetchall()
    qts = [float(r[0]) for r in q]
    n, stake = float(b["contracts"] or 0), float(b["stake_usd"] or 0)
    if b["exit_price"] is not None:
        x = float(b["exit_price"])
        was, how = n * (x - fee_per_contract(x)) - stake, f"sold {x:.2f} {datetime.datetime.fromtimestamp(float(b['exit_ts']), ET).strftime('%M:%S')}"
        stop_at = float(b["exit_ts"])
    elif b["outcome"] in ("yes", "no"):
        was, how, stop_at = (n if b["outcome"] == side else 0.0) - stake, ("won " if b["outcome"] == side else "LOST") + " held", close
    else:
        continue
    need = -1.0 if side == "yes" else 1.0  # a YES holder is flipped when the spot is under the strike, a NO holder when it is at or over it
    crossed, since, sale = None, None, None
    at_buy = (g.get(buy, strike) - strike) * 1e4
    for t in range(buy, int(close) - 8, STEP):
        if t not in g:
            continue
        past = (g[t] - strike) * need  # how far past the strike against the bet, in log terms (positive = flipped)
        left = close - t
        if past <= 0:
            since = None
            continue
        if crossed is None:
            crossed = t
        span = int(left // STEP * STEP)
        hits = tries = 0
        for u in range(t - 3600, t - span + 1, STEP):
            if u in g and u + span in g:
                tries += 1
                hits += 1 if (g[u + span] - g[u]) * -need >= past else 0
        if tries < 60:
            continue
        share = hits / tries
        i = bisect.bisect_right(qts, t) - 1
        if i < 0:
            continue
        yb, ya = float(q[i][1] or 0), float(q[i][2] or 0)
        bid = yb if side == "yes" else (1.0 - ya if ya > 0 else 0.0)
        net = bid - fee_per_contract(bid) if bid > 0 else 0.0
        if bid >= 0.05 and share < net:
            since = since or t
            if t - since >= SHARE * left:
                sale = (t, bid, share, past * 1e4, left)
                break
        else:
            since = None
    tot["n"] += 1
    tot["was"] += was
    stamp = datetime.datetime.fromtimestamp(float(b["ts"]), ET).strftime("%H:%M:%S")
    cross = datetime.datetime.fromtimestamp(crossed, ET).strftime("%M:%S") if crossed else "never"
    if sale and sale[0] < stop_at:
        t, bid, share, past, left = sale
        new = n * (bid - fee_per_contract(bid)) - stake
        tot["sold"] += 1
        tot["now"] += new
        what = f"{datetime.datetime.fromtimestamp(t, ET).strftime('%M:%S')} at {bid:.2f}, {past:.0f}bp past, back in {share:.0%} of the hour, {left:.0f}s left"
        print(f"{stamp} {b['ticker'][2:5]:4s} {side:3s} {float(b['ask']):.2f} | {at_buy:+6.1f} bp             | {cross:8s} | {what:39s} | {how:15s} {was:+7.2f} | {new - was:+7.2f}")
    else:
        tot["now"] += was
        print(f"{stamp} {b['ticker'][2:5]:4s} {side:3s} {float(b['ask']):.2f} | {at_buy:+6.1f} bp             | {cross:8s} | {'no sale':39s} | {how:15s} {was:+7.2f} |   +0.00")
print(f"bets {tot['n']}: what happened {tot['was']:+.2f}; with the flip rule {tot['now']:+.2f} ({tot['sold']} sold by it); change {tot['now'] - tot['was']:+.2f}")
print("DONE")
