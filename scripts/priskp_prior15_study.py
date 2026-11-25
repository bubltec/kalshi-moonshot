"""Did the 15 minutes before the buy tell the losers from the winners? (2026-11-24; user: "if it just looked at the 15min before it would be clear not to buy when it did", on the 22:00 and 22:15 crypto bets.)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, the spot's behaviour over the 15 minutes before the buy, read from the bet's side (a rise is with a YES bet, a fall is with a NO bet), in the spot's own typical minutes: drift (log change from 15 minutes before to the buy), and the bet's cushion at the buy over the strike's side. Do the bets that lost money (as they ran: sold or settled) differ from those that made money?
HONEST LIMITS: n is one day's bets (105), mostly one regime; the split at the median is chosen after seeing the results and said to be so; 'as they ran' mixes the lane's exits with the entry; no price paid is separated. A lead only; a rule on this would go to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_prior15_study.py"""
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
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


rows = st.execute(
    "SELECT ticker, side, ts, stake_usd, contracts, exit_ts, exit_price FROM decisions WHERE lane='priskp' AND action='bet' AND mode='trade' AND ts > ? ORDER BY ts",
    (datetime.datetime(2026, 11, 24, 5, tzinfo=datetime.timezone.utc).timestamp(),),
).fetchall()
out = []
for r in rows:
    try:
        tk, side, n = r["ticker"], r["side"], float(r["contracts"])
        pay = float(r["stake_usd"]) / n
        cost = pay + fee(pay)
        if r["exit_ts"]:
            net = float(r["exit_price"]) - fee(float(r["exit_price"])) - cost
        else:
            m = market(tk)
            res = m.get("result")
            if not res:
                continue
            net = (1.0 if res == side else 0.0) - cost
        ser = re.match(r"(KX[A-Z0-9]+15M)", tk).group(1)
        ix = index_for_series(ser)
        rows_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 960.0, r["ts"])).fetchall()
        if len(rows_) < 300:
            continue
        T = [float(x[0]) for x in rows_]
        V = [float(x[1]) for x in rows_]
        rets = [math.log(V[i + 1] / V[i]) for i in range(len(V) - 1) if T[i + 1] - T[i] <= 1.5 and V[i] > 0]
        unit = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(60.0)
        sign = 1.0 if side == "yes" else -1.0
        first = next(V[i] for i in range(len(V)) if T[i] >= r["ts"] - 900.0)
        drift = sign * math.log(V[-1] / first) / unit  # in typical minutes, with the bet's side positive
        # the last 5 minutes against the first 10: is the move still going the bet's way?
        mid = next(V[i] for i in range(len(V)) if T[i] >= r["ts"] - 300.0)
        late = sign * math.log(V[-1] / mid) / unit
        out.append((tk, side, pay, drift, late, net, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a settled or sold result and a 15-minute spot history")
out.sort(key=lambda x: x[3])
half = len(out) // 2


def line(label, g):
    if not g:
        return
    tot = sum(x[5] * x[6] for x in g)
    lost = sum(1 for x in g if x[5] < 0)
    print(f"{label:34s} n {len(g):3d}  net ${tot:+8.2f}  lost {lost:3d} ({100.0 * lost / len(g):.0f}%)  mean net/contract {sum(x[5] for x in g) / len(g):+.3f}")


line("all", out)
line("prior-15 drift: against the bet's side (lower half)", out[:half])
line("prior-15 drift: with the bet's side (upper half)", out[half:])
q = len(out) // 4
line("  lowest quarter of drift", out[:q])
line("  highest quarter of drift", out[-q:])
neg = [x for x in out if x[3] < 0]
line("drift negative (spot moved against the side)", neg)
line("drift positive", [x for x in out if x[3] >= 0])
out.sort(key=lambda x: x[4])
line("last 5 min against the side (lower half)", out[:half])
line("last 5 min with the side (upper half)", out[half:])
print("\nthe 22:00 and 22:15 bets:")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0]:
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} drift15 {x[3]:+6.2f}u last5 {x[4]:+6.2f}u net {x[5]:+.3f}")
