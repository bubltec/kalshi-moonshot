"""Buy on confirmation, not after a clock (2026-11-24; user, on the fixed 2-minute settle_hold: "what's the point in adding a fixed wait if there's no logic to it. That's just a mutation of the time lapsed").
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, treat the real buy as the first moment the lane wanted to buy, note the spot then, and buy the same side at its ask at the FIRST moment after it when the spot has moved the bet's way by at least ONE TYPICAL MINUTE of its own (the root-mean-square 1 s move over the hour before the buy x sqrt(60)): a confirmation in the spot's own units, no clock. Not bought if that never happens before 60 s before the close, or the ask at that moment is outside the lane's band 0.55-0.85. Held to the market's own result, fee on the buy. Compare with the same bets bought for real, and with the fixed 117 s wait that is live now (bought only if the spot had not moved against the bet).
HONEST LIMITS: held to settlement (the lane's exits are out of it); the other priskp checks are not re-run at the later time; one day (106 bets) in one regime; ONE typical minute is the one setting I chose before running and I ran no other. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_confirm_study.py"""
import datetime
import functools
import json
import math
import re
import traceback
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

print = functools.partial(print, flush=True)
LO, HI, FIXED = 0.55, 0.85, 117.0
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


rows = st.execute(
    "SELECT ticker, side, ts, stake_usd, contracts FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (datetime.datetime(2026, 11, 24, 5, tzinfo=datetime.timezone.utc).timestamp(),),
).fetchall()
out = []
for r in rows:
    try:
        tk, side, n = r["ticker"], r["side"], float(r["contracts"])
        pay = float(r["stake_usd"]) / n
        res = market(tk).get("result")
        if not res:
            continue
        win = 1.0 if res == side else 0.0
        real = win - pay - fee(pay)
        ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", tk).group(1))
        close = (int(r["ts"]) // 900 + 1) * 900.0
        ticks_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 3600.0, close)).fetchall()
        T = [float(x[0]) for x in ticks_]
        V = [float(x[1]) for x in ticks_]
        pre = [(T[i], V[i]) for i in range(len(T)) if T[i] <= r["ts"]]
        rets = [math.log(pre[i + 1][1] / pre[i][1]) for i in range(len(pre) - 1) if pre[i + 1][0] - pre[i][0] <= 1.5 and pre[i][1] > 0]
        if len(rets) < 500:
            continue
        unit = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(60.0)  # one typical minute, as a log move
        v0 = pre[-1][1]
        sign = 1.0 if side == "yes" else -1.0
        when = None
        for t, v in zip(T, V):
            if t <= r["ts"]:
                continue
            if t >= close - 60.0:
                break
            if sign * math.log(v / v0) >= unit:
                when = t
                break

        def buy_at(t):
            q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, t, t + 20.0)).fetchall()
            if not q:
                return None
            a = float(q[0]["ask"]) if side == "yes" else 1.0 - float(q[0]["bid"])
            return (win - a - fee(a)) if LO <= a <= HI else None

        conf = buy_at(when) if when else None
        late = [v for t, v in zip(T, V) if t <= r["ts"] + FIXED]
        ok = (late[-1] >= v0) if side == "yes" else (late[-1] <= v0)
        fx = buy_at(r["ts"] + FIXED) if ok else None
        out.append((tk, side, real, conf, (when - r["ts"]) if when else None, fx, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result")
tot = lambda g, i: sum(x[i] * x[6] for x in g)
print(f"real buys held to the result: ${tot(out, 2):+.2f} on {len(out)}")
c = [x for x in out if x[3] is not None]
f = [x for x in out if x[5] is not None]
print(f"confirmation (one typical minute): bought {len(c)} bets, ${tot(c, 3):+.2f}; the same bets bought for real ${tot(c, 2):+.2f}; median wait {sorted(x[4] for x in c)[len(c) // 2]:.0f} s" if c else "confirmation: no buys")
print(f"fixed 117 s with no move against: bought {len(f)} bets, ${tot(f, 5):+.2f}; the same bets bought for real ${tot(f, 2):+.2f}")
both = [x for x in out if x[3] is not None and x[5] is not None]
print(f"bought by both rules: {len(both)} bets")
print("\nthe 22:00 and 22:15 bets (and ZEC 23:00):")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0] or "KXZEC15M-26NOV242300" in x[0]:
        print(f"{x[0][:24]:24s} {x[1]:3s} real {x[2]:+.3f}  confirmation " + (f"{x[3]:+.3f} after {x[4]:.0f}s" if x[3] is not None else ("not bought" + (f" (confirmed after {x[4]:.0f}s but the ask was out of the band)" if x[4] is not None else " (never confirmed)"))))
