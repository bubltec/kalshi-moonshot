"""Do bets bought later in the market do better? (2026-11-24; user, on ZEC 23:00 NO bought at minute 5: "I'd wait until later in the market to see what it's going to do".)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC (as they ran: sold or settled), by the minute of the market the bet was bought (minutes since the market's open), the net in dollars, the loss share and the net per contract; and the same held to the market's own result.
HONEST LIMITS: one day (105 bets) in one regime; later buys have less time to come back and a bet bought late is a different price; 'as they ran' mixes the lane's exits with the entry; bins are fixed thirds of the market (minutes 0-5, 5-10, 10-15), chosen before the result. A lead only.
Run on the host: scripts/cloud.sh run scripts/priskp_buy_minute_study.py"""
import datetime
import functools
import json
import traceback
import urllib.request

from kalshi_moonshot import config, db

print = functools.partial(print, flush=True)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)


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
        n = float(r["contracts"])
        pay = float(r["stake_usd"]) / n
        cost = pay + fee(pay)
        res = market(r["ticker"]).get("result")
        if not res:
            continue
        held = (1.0 if res == r["side"] else 0.0) - cost
        net = held if not r["exit_ts"] else float(r["exit_price"]) - fee(float(r["exit_price"])) - cost
        minute = (r["ts"] - (int(r["ts"]) // 900) * 900) / 60.0
        out.append((minute, net, held, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets")


def line(label, g):
    if not g:
        print(f"{label:28s} n   0")
        return
    tot = sum(x[1] * x[3] for x in g)
    th = sum(x[2] * x[3] for x in g)
    lost = sum(1 for x in g if x[1] < 0)
    print(f"{label:28s} n {len(g):3d}  as run ${tot:+8.2f} lost {lost:3d} ({100.0 * lost / len(g):.0f}%) per contract {sum(x[1] for x in g) / len(g):+.3f}   held ${th:+8.2f}")


line("all", out)
for lo, hi in ((0, 3), (3, 5), (5, 8), (8, 11), (11, 15)):
    line(f"bought in minutes {lo}-{hi}", [x for x in out if lo <= x[0] < hi])
