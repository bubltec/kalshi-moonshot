"""What if the buy had waited two minutes? (2026-11-24; user, on the 22:15 NO bets: "if we held for 2 minutes we would have seen the trend change".)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, buy the same side at its ask WAIT seconds after the real buy instead (skipped when that ask is outside the lane's price band 0.55-0.85, or no quote), both held to the market's own result, fee on the buy. Compare the delayed buy with the real buy held to the same result, bet by bet and in dollars; also the two together when the delayed buy is made only if the spot has not moved against the bet in the wait (a 'trend change' read: the spot at the delayed time on the bet's side of where it stood at the real buy).
HONEST LIMITS: held to settlement for both, so the lane's exits are out of it; the other priskp checks are not re-run at the later time (only the price band), so some delayed buys would have been refused or made at another time by the lane; one day (105 bets) in one regime; WAIT = 120 is the user's number and the 0.55-0.85 band is the lane's own. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_hold_two_minutes_study.py [WAIT_SECONDS]"""
import datetime
import functools
import json
import re
import sys
import traceback
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

print = functools.partial(print, flush=True)
WAIT = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
LO, HI = 0.55, 0.85
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
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, r["ts"] + WAIT, r["ts"] + WAIT + 20.0)).fetchall()
        close = (int(r["ts"]) // 900 + 1) * 900.0
        if not q or r["ts"] + WAIT >= close - 60.0:
            out.append((tk, side, pay, real, None, None, n))
            continue
        a = float(q[0]["ask"]) if side == "yes" else 1.0 - float(q[0]["bid"])
        if not (LO <= a <= HI):
            out.append((tk, side, pay, real, "band", None, n))
            continue
        delayed = win - a - fee(a)
        ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", tk).group(1))
        sp = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 5.0, r["ts"] + WAIT + 2.0)).fetchall()
        v0 = float(sp[0][1])
        v1 = float(sp[-1][1])
        held_way = (v1 >= v0) if side == "yes" else (v1 <= v0)  # the spot did not move against the bet in the wait
        out.append((tk, side, pay, real, delayed, held_way, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result; wait {WAIT:.0f} s; band {LO}-{HI}")
done = [x for x in out if isinstance(x[4], float)]
skipped = [x for x in out if not isinstance(x[4], float)]
tot = lambda g, i: sum(x[i] * x[6] for x in g)
print(f"real buys held to the result, all {len(out)}: ${tot(out, 3):+.2f}")
print(f"delayed buys made {len(done)} (skipped {len(skipped)}: ask out of the band or no quote):")
print(f"  the same {len(done)} bets bought for real: ${tot(done, 3):+.2f};   bought {WAIT:.0f} s later: ${tot(done, 4):+.2f}")
ok = [x for x in done if x[5]]
print(f"delayed buys where the spot did not move against the bet in the wait: {len(ok)}")
print(f"  the same bets bought for real: ${tot(ok, 3):+.2f};   bought later: ${tot(ok, 4):+.2f}")
bad = [x for x in done if not x[5]]
print(f"delayed buys where the spot moved against the bet in the wait (a 'trend change'): {len(bad)}")
print(f"  bought for real: ${tot(bad, 3):+.2f};   bought later: ${tot(bad, 4):+.2f}")
print("\nthe 22:00 and 22:15 bets:")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0]:
        late = f"{x[4]:+.3f} (spot {'with' if x[5] else 'AGAINST'} in the wait)" if isinstance(x[4], float) else f"no delayed buy ({x[4] or 'no quote'})"
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} real held {x[3]:+.3f} delayed {late}")
