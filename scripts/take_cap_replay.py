"""The profit take's late cap, relaxed (2026-11-14, user: "the time remaining seems to be making the profit sell too sensitive, we need to relax it a bit").
take_bar = (the share of the market gone) x room, and once what is left to win is inside the market's swing over the time left the bar is capped at `c` x room (live c = 0.5, curverisk.take_bar). Only the cap factor c moves here; the clock bar, the swing test, the fee test and the slope test (any rise, live since 21:30) are the live ones.
Same bets: every live crypto_multiscale profit take since the take went live whose sale the late cap made (the reason says "half the room"). Held to the bet's real sale second, the take is re-walked second by second on the taped quotes (the side's bid; the typical minute re-read from the market's mids every 5 s): the first second it fires under c is the variant's sale, else the bet settles. Fee on both legs. Compared with what really happened. A bet whose take is not fired by the late cap is unchanged by c. Settings chosen after seeing the results are said to be so.
Run on the host: python scripts/take_cap_replay.py"""
import bisect
import datetime
import json
import math
import urllib.request

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 18, 0, tzinfo=datetime.UTC).timestamp()
CAPS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)  # 1.0 = no cap at all
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute(
    "SELECT ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, close_time FROM decisions WHERE lane='crypto_multiscale' AND action='bet' AND stake_usd>0 AND ts>? AND exit_reason LIKE ? ORDER BY exit_ts",
    (SINCE, "curve risk exit (profit take)%")).fetchall()


def result_of(ticker, outcome):
    if outcome in ("yes", "no"):
        return outcome
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", timeout=10) as f:
            r = json.load(f)["market"].get("result")
        return r if r in ("yes", "no") else None
    except Exception:
        return None


def bar_with_cap(gain, room, left, unit, c):
    bar = min(max(1.0 - left / 900.0, 0.0), 1.0) * room
    if unit is not None and unit > 0 and left > 0 and room - gain <= unit * math.sqrt(left / 60.0):
        bar = min(bar, c * room)
    return bar


out = {c: [0, 0.0, 0.0, 0, 0] for c in CAPS}  # bets, real result, variant result, better, worse
print("late-cap takes (sold by the cap), the real sale vs the variants' sales: time left, price")
for r in rows:
    if "half the room" not in (r["exit_reason"] or ""):
        continue
    side, n, cost = r["side"], float(r["contracts"]), float(r["stake_usd"])
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    t0 = float(r["exit_ts"])
    res = result_of(r["ticker"], r["outcome"])
    if res is None:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t0 - 1300.0, close)).fetchall()
    ts = [float(x[0]) for x in q]
    bd = [(float(x[1]) if side == "yes" else 1.0 - float(x[2])) for x in q]
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2.0) for x in q]
    if len(ts) < 50:
        continue
    at = lambda x: bd[max(bisect.bisect_right(ts, x) - 1, 0)]
    real = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    entry = cost / n
    basis = entry + fee_per_contract(entry)
    line = []
    for c in CAPS:
        hist, unit, unit_at, got = [], None, 0.0, None
        t = t0 - 60.0
        while t < close - 8.0:
            bid, left = at(t), close - t
            look = 0.08 * left
            hist.append((t, bid))
            while hist and hist[0][0] < t - max(60.0, 2 * look):
                hist.pop(0)
            if t - unit_at >= 5.0:
                unit_at = t
                unit = quoteshock.typical_minute(mids[: bisect.bisect_right(ts, t)], t, 900.0)
            if t >= t0 - 1.0:  # held to the real sale second: the variant only decides from there
                net = bid - fee_per_contract(bid)
                gain, room = net - basis, 1.0 - basis
                if gain > 0 and bid >= 0.05 and gain > fee_per_contract(bid) and gain >= bar_with_cap(gain, room, left, unit, c) and not curverisk.still_rising(hist, t, look, unit):
                    got = (t, bid)
                    break
            t += 1.0
        if got:
            v = n * (got[1] - fee_per_contract(got[1])) - cost
            line.append(f"c={c:.1f}: {close - got[0]:.0f}s @{got[1]:.3f}")
        else:
            v = (n if res == side else 0.0) - cost
            line.append(f"c={c:.1f}: settles")
        k = out[c]
        k[0] += 1; k[1] += real; k[2] += v; k[3] += v > real + 1e-9; k[4] += v < real - 1e-9
    print(f"  {r['ticker'][2:-3]:22s} {side:3s} real {close - t0:4.0f}s left @{float(r['exit_price']):.3f}, ended {res}: " + " | ".join(line))
print()
for c in CAPS:
    k = out[c]
    print(f"cap {c:.1f} x room: {k[0]} bets, real {k[1]:+.2f}, variant {k[2]:+.2f}, change {k[2]-k[1]:+.2f}; better {k[3]}, worse {k[4]}")
print("DONE")
