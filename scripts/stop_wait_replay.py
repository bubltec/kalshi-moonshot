"""The stop's wait, old against new, replayed on the live lane's own bets (2026-11-14).
Old: once the max loss is reached the stop waits half the time left. New (curverisk.stop_wait): the market's recovery time, less the deeper the chance is under the 0.50 line, times the share of the market still to run, never over half the time left.
Everything else is the live exit as it stands and is the same in both columns: the max loss (the loss is at least 0.5 x the chance), the hour view (hold while the chance read from the last hour of the index against its value at the window's open is 0.4 or more), the swing hold (hold while the loss is under 1.5 x the contract's typical minute x sqrt(minutes left) and the chance is 0.10 or more), no sale under a bid of 0.05 or in the last 8 s; a lapse of the condition restarts the wait.
The recovery time is the lane's default (15 s): the live exit uses the market's own measured one when the last hour has five fast falls, which this replay does not rebuild, so the new wait here is a floor on what live would wait (the measured time can be up to 120 s).
Each bet runs from its buy to its real exit or the close; a replayed loss sale replaces what happened only if it comes first. Sales fill at the bid of that second net of the fee. Same bets; one day; the bets are the ones the lane chose.
Run on the host: scripts/cloud.sh run scripts/stop_wait_replay.py"""
import bisect
import datetime
import math
import os

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.commodities import instrument_for, load_instruments
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.load()
store = Store(s.db_path)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = datetime.timezone(datetime.timedelta(hours=-5))
MAX_LOSS, LINE, FLOOR, BASE = 0.5, 0.5, 0.10, 15.0
CONFIRM, SWING, SHARE = float(os.environ.get("CONFIRM", "0.4")), float(os.environ.get("SWING", "1.5")), float(os.environ.get("SHARE", "0.5"))  # the live holds; CONFIRM=0.5 SWING=0.5 is the tighter pair proposed the same evening
print(f"holds: hour view holds at {CONFIRM}, swing {SWING}, stop wait share {SHARE}")


def stop_wait(q, left):
    severity = min(1.0, max(0.0, (LINE - q) / LINE))
    return max(0.0, min(BASE * (1.0 - severity) * min(1.0, left / 900.0), SHARE * left))


bets = store.db.execute(
    "SELECT id, ts, ticker, side, ask, contracts, stake_usd, exit_price, exit_ts, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (float(os.environ.get("SINCE", "1794668400")),),
).fetchall()
tot = {"n": 0, "was": 0.0, "old": 0.0, "new": 0.0, "old_sold": 0, "new_sold": 0}
print("bought   coin side paid | happened              | old wait (half the time left)   | new wait                        ")
for b in bets:
    inst = instrument_for(b["ticker"].split("-")[0], load_instruments(s.data_dir))
    if inst is None or not inst.symbol.startswith("kp:"):
        continue
    side = b["side"]
    close = datetime.datetime.fromisoformat(b["close_time"]).timestamp()
    n, stake = float(b["contracts"] or 0), float(b["stake_usd"] or 0)
    if n <= 0:
        continue
    if b["exit_price"] is not None:
        x = float(b["exit_price"])
        was, how, end = n * (x - fee_per_contract(x)) - stake, f"sold {x:.2f} {datetime.datetime.fromtimestamp(float(b['exit_ts']), ET).strftime('%M:%S')}", float(b["exit_ts"])
    elif b["outcome"] in ("yes", "no"):
        was, how, end = (n if b["outcome"] == side else 0.0) - stake, ("won  held" if b["outcome"] == side else "LOST held"), close
    else:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (b["ticker"], close - 900 - 60, close)).fetchall()
    qts = [float(r[0]) for r in q]
    mids = [(float(r[0]), (float(r[1]) + float(r[2])) / 2.0) for r in q if r[1] and r[2] and float(r[1]) > 0 and float(r[2]) > 0]
    ix = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (inst.symbol.removeprefix("kp:"), close - 900 - 3800, close)).fetchall()
    its, ivs = [float(r[0]) for r in ix], [float(r[1]) for r in ix]
    j = bisect.bisect_left(its, close - 900.0)
    ref = ivs[j] if j < len(its) and its[j] - (close - 900.0) <= 60 else 0.0
    basis = stake / n  # what the bet cost a contract, fee included
    out = {}
    for name in ("old", "new"):
        since, sale = 0.0, None
        for t in range(int(math.ceil(float(b["ts"]))) + 1, int(min(end, close - 8))):
            i = bisect.bisect_right(qts, t) - 1
            if i < 0:
                continue
            yb, ya = float(q[i][1] or 0), float(q[i][2] or 0)
            bid = yb if side == "yes" else (1.0 - ya if ya > 0 else 0.0)
            if bid <= 0:
                since = 0.0
                continue
            net = bid - fee_per_contract(bid)
            pnl, left = (net - basis) / basis, close - t
            k = bisect.bisect_right(its, t)
            lc = curverisk.long_chance(its[:k], ivs[:k], float(t), ref, side, left) if ref > 0 else None
            if lc is not None and lc >= CONFIRM:
                since = 0.0
                continue
            if pnl < 0 and net >= FLOOR:
                unit = quoteshock.typical_minute([m for m in mids if m[0] <= t], float(t))
                if curverisk.swing_hold(basis - net, unit, left, SWING):
                    since = 0.0
                    continue
            if not (curverisk.stopped(pnl, net, MAX_LOSS) and bid >= 0.05):
                since = 0.0
                continue
            since = since or float(t)
            wait = SHARE * left if name == "old" else stop_wait(net, left)
            if t - since >= wait:
                sale = (t, bid, left)
                break
        if sale:
            t, bid, left = sale
            out[name] = (n * (bid - fee_per_contract(bid)) - stake, f"sells {bid:.2f} at {datetime.datetime.fromtimestamp(t, ET).strftime('%M:%S')}, {left:.0f}s left")
            tot[name + "_sold"] += 1
        else:
            out[name] = (was, "as happened")
    tot["n"] += 1
    tot["was"] += was
    tot["old"] += out["old"][0]
    tot["new"] += out["new"][0]
    if out["old"][1] != "as happened" or out["new"][1] != "as happened" or was < -5:
        stamp = datetime.datetime.fromtimestamp(float(b["ts"]), ET).strftime("%H:%M:%S")
        print(f"{stamp} {b['ticker'][2:5]:4s} {side:3s} {basis:.2f} | {how:14s} {was:+7.2f} | {out['old'][1]:24s} {out['old'][0]:+7.2f} | {out['new'][1]:24s} {out['new'][0]:+7.2f}")
print(f"bets {tot['n']}: what happened {tot['was']:+.2f} | old wait replayed {tot['old']:+.2f} ({tot['old_sold']} loss sales) | new wait {tot['new']:+.2f} ({tot['new_sold']} loss sales) | new against what happened {tot['new'] - tot['was']:+.2f}")
print("DONE")
