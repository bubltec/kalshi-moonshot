"""Why did a bet that was ahead not take its profit? Second-by-second trace of the live profit take on given bets (2026-11-15, BNB and DOGE 13:15, YES, both reached a gain, both stopped out later at -55%).

For each bet, from its buy on the taped quotes (the side's bid), the take's own tests as watcher._risk_decide runs them: the gain over the price paid and the fee, its share of the room, the bar (curverisk.take_bar) and whether take_profit wants a sale, and, when it does, whether the slope test (curverisk.still_rising over 8% of the time left, within the market's noise) holds it. Prints the seconds where the take wanted a sale and the reason it did not sell, and the best the bet ever was. The cushion check (spot's chance against the bid) is not walked.

Run on the host: TICKERS=KXBNB15M-26NOV151315-15,KXDOGE15M-26NOV151315-15 python scripts/take_missed_trace.py
"""
import bisect
import datetime
import os

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
f = lambda t: datetime.datetime.fromtimestamp(float(t), ET).strftime("%H:%M:%S")
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
for tk in os.environ["TICKERS"].split(","):
    b = st.execute("SELECT ts, side, contracts, stake_usd, exit_ts, exit_price, close_time FROM decisions WHERE ticker=? AND lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND mode='trade' ORDER BY ts LIMIT 1", (tk,)).fetchone()
    if not b:
        print(tk, "no bet"); continue
    n, entry = float(b["contracts"]), float(b["stake_usd"]) / float(b["contracts"]); basis = entry + fee_per_contract(entry); room = 1.0 - basis
    close = datetime.datetime.fromisoformat(str(b["close_time"]).replace("Z", "+00:00")).timestamp()
    end = float(b["exit_ts"]) if b["exit_ts"] else close - 8.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (tk, float(b["ts"]) - 1200.0, close)).fetchall()
    ts = [float(x[0]) for x in q]; bid = [(float(x[1]) if b["side"] == "yes" else 1.0 - float(x[2])) for x in q]; mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    at = lambda t: bid[max(bisect.bisect_right(ts, t) - 1, 0)]
    print(f"\n{tk[2:-3]} {b['side']} bought {f(b['ts'])} at {entry:.2f} (basis {basis:.3f}, room {room:.3f}), sold {f(end) if b['exit_ts'] else 'never'} at {b['exit_price']}")
    hist, unit, ua, best, rows, t = [], None, 0.0, (-9, 0), [], float(b["ts"]) + 1.0
    while t < end:
        bd, left = at(t), close - t; look = 0.08 * left
        hist.append((t, bd))
        while hist and hist[0][0] < t - max(60.0, 2 * look): hist.pop(0)
        if t - ua >= 5.0:
            ua = t; unit = quoteshock.typical_minute(mids[: bisect.bisect_right(ts, t)], t, 900.0)
        net = bd - fee_per_contract(bd); gain = net - basis
        if gain > best[0]: best = (gain, t)
        if gain > 0 and bd >= 0.05:
            bar = curverisk.take_bar(gain, room, left, 900.0, unit); wants = curverisk.take_profit(gain, room, left, 900.0, fee_per_contract(bd), unit)
            rising = curverisk.still_rising(hist, t, look, unit, False) if wants else None
            rows.append((t, bd, gain, gain / room, bar / room, wants, rising, left))
        t += 1.0
    print(f"  best: gain {best[0]:+.3f} a contract ({best[0] / room * 100:.0f}% of room, ${best[0] * n:+.2f} on {n:.0f} contracts) at {f(best[1])}")
    wanted = [r for r in rows if r[5]]
    print(f"  seconds in profit {len(rows)}; seconds the take wanted a sale {len(wanted)}; of those held by the slope test (still rising) {sum(1 for r in wanted if r[6])}, not held {sum(1 for r in wanted if r[6] is False)}")
    step = max(1, len(rows) // 12)
    print("  in-profit path (time bid gain%room bar%room wants rising left): " + " | ".join(f"{f(r[0])[3:]} {r[1]:.2f} {r[3] * 100:.0f}/{r[4] * 100:.0f} {'W' if r[5] else '-'}{'R' if r[6] else ''} {r[7]:.0f}s" for r in rows[::step]))
print("DONE")
