"""Where in the hour's range was the spot when the bet was bought? (2026-11-24; user, with the 1H charts of DOGE and SOL 22:15 NO bets: neither showed the trend down at the buy; both charts show the spot at the hour's low just when the bet went in, then a bounce through the target.)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC (as they ran: sold or settled), the spot's position at the buy inside the range it covered over the previous hour: (spot - low) / (high - low), signed so that 1.0 is at the extreme in the bet's direction (the hour's low for a NO bet, its high for a YES bet) and 0.0 is at the other extreme. Also the hour's net change in the same terms as the earlier study, for comparison. Do bets bought at the extreme in their own direction lose more?
HONEST LIMITS: one day's bets (105) in one regime; quartiles of the reading, cut after seeing the user's charts and said to be so; 'as they ran' mixes the lane's exits with the entry; the bets left out are not replaced. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_range_position_study.py"""
import datetime
import functools
import json
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
            res = market(tk).get("result")
            if not res:
                continue
            net = (1.0 if res == side else 0.0) - cost
        ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", tk).group(1))
        ticks_ = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 3600.0, r["ts"])).fetchall()
        if len(ticks_) < 1000:
            continue
        V = [float(x[0]) for x in ticks_]
        lo, hi = min(V), max(V)
        if hi <= lo:
            continue
        pos = (V[-1] - lo) / (hi - lo)  # 0 = at the hour's low, 1 = at its high
        ext = 1.0 - pos if side == "no" else pos  # 1 = at the extreme in the bet's own direction
        out.append((tk, side, pay, ext, net, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result and an hour of spot history")


def line(label, g):
    if not g:
        print(f"{label:52s} n   0")
        return
    tot = sum(x[4] * x[5] for x in g)
    lost = sum(1 for x in g if x[4] < 0)
    print(f"{label:52s} n {len(g):3d}  net ${tot:+8.2f}  lost {lost:3d} ({100.0 * lost / len(g):.0f}%)  per contract {sum(x[4] for x in g) / len(g):+.3f}")


line("all", out)
srt = sorted(out, key=lambda x: x[3])
q = len(srt) // 4
for k in range(4):
    g = srt[k * q:(k + 1) * q if k < 3 else len(srt)]
    line(f"quartile {k + 1} of 'at the extreme in the bet's direction'" + (" (lowest)" if k == 0 else " (highest: bought at the extreme)" if k == 3 else ""), g)
top = srt[3 * q:]
cut = srt[3 * q][3]
line("all but the top quartile", srt[:3 * q])
print(f"(the top quartile starts at {cut:.2f} of the way to the extreme)")
print("\nthe 22:00 and 22:15 bets (1.00 = at the extreme in the bet's direction):")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0]:
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} at {x[3]:.2f} of the way to the extreme   net {x[4]:+.3f}")
