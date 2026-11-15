"""Is something stopping the profit take from selling? (2026-11-14 evening, user: "something is causing us not to take profit. taking profit early is supposed to happen").
For every live crypto_multiscale bet since the take went live: the first second the take WANTED to sell (in profit, bid at or over the minimum, gain over the fee to buy back and over the take bar, the typical minute re-read from the quotes), ignoring the slope and the cushion; what the bot really did (sold by the take, at which second; another exit; or held to settlement); and the bet's value if sold at that first wanting second against what it really got (fee on both legs). A bet that wanted to sell and ended worse than selling then is a take that did not happen. Bets are split at 21:30:52 ET, when the slope test became "any rise".
Run on the host: python scripts/take_wanted_check.py"""
import bisect
import datetime

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
SPLIT = datetime.datetime(2026, 11, 14, 21, 30, 52, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
bets = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
agg = {"before": [0, 0, 0, 0.0], "after": [0, 0, 0, 0.0]}  # bets, wanted, wanted and ended worse, dollars lost against selling then
print("bet                    bought  first wanted (left, bid, value)   really: how, value | worse by")
for b in bets:
    close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp()
    t0 = float(b["ts"]); n = float(b["contracts"]); cost = float(b["stake_usd"]); side = b["side"]
    end = float(b["exit_ts"]) if b["exit_ts"] is not None else close - 8.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (b["ticker"], t0 - 1000.0, close)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(x[0]) for x in q]; bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    at = lambda x: bd[max(bisect.bisect_right(ts, x) - 1, 0)]
    entry = cost / n; basis = entry + fee_per_contract(entry); room = 1.0 - basis
    unit, unit_at, want = None, 0.0, None
    t = t0 + 1.0
    while t < end:
        bid, left = at(t), close - t
        if t - unit_at >= 5.0:
            unit_at = t; unit = quoteshock.typical_minute(mids[: bisect.bisect_right(ts, t)], t, 900.0)
        net = bid - fee_per_contract(bid); gain = net - basis
        if gain > 0 and bid >= 0.05 and curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bid), unit):
            want = (t, bid, left, n * net - cost); break
        t += 1.0
    if b["exit_price"] is not None:
        really = n * (float(b["exit_price"]) - fee_per_contract(float(b["exit_price"]))) - cost; how = "sold " + ("take" if "profit take" in (b["exit_reason"] or "") else "stop" if "max loss" in (b["exit_reason"] or "") else "other")
    elif b["outcome"] in ("yes", "no"):
        really = (n if b["outcome"] == side else 0.0) - cost; how = "settled " + ("won" if b["outcome"] == side else "lost")
    else:
        continue
    g = agg["before" if t0 < SPLIT else "after"]; g[0] += 1
    if want:
        g[1] += 1
        worse = want[3] - really
        if worse > 0.5:
            g[2] += 1; g[3] += worse
        print(f"{b['ticker'][2:-3]:22s} {entry:5.2f}   {want[2]:4.0f}s {want[1]:.3f} {want[3]:+7.2f}   {how:13s} {really:+7.2f} | {worse:+.2f}{'  <-- ended worse than selling then' if worse > 0.5 else ''}")
    else:
        print(f"{b['ticker'][2:-3]:22s} {entry:5.2f}   never wanted to sell                  {how:13s} {really:+7.2f}")
print()
for k in ("before", "after"):
    g = agg[k]
    print(f"{k} 21:30:52: {g[0]} bets, {g[1]} had a moment the take wanted to sell, {g[2]} of those ended worse than selling then (total {g[3]:+.2f} dollars)")
print("DONE")
