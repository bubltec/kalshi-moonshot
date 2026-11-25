"""Hold the buy until the spot shows a direction (2026-11-24; user, after the 22:15 NO bets: "what I think it means is hold. Doesn't mean to pass but if we don't see a direction yet we shouldn't bet yet").
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC (as they ran: sold or settled), at the buy, did the spot show a DIRECTION over the previous 15 minutes and over the previous hour, and was it the bet's way? Direction = the path's efficiency (the net move over the total distance travelled, 10 s samples; 1.0 is a straight line, near 0 is chop) compared with the same measure over this series' own earlier windows of the same length in the 8 hours before (its median); a window with efficiency over its own median is 'directional', else 'chop'. With the bet = the net move runs the bet's way (a fall for NO, a rise for YES). Cells: directional and with the bet; directional and against; chop. Report n, net, loss share per cell, and the lane's net if the chop bets are left out.
HONEST LIMITS: a held bet is not scored as bought later (that needs the later quote and fill; this leaves it out), so a 'chop' bet's real alternative is a later buy at another price, better or worse; one day's bets (105) in one regime; the median comparison is fixed before the result but the two windows (15 minutes and the hour) are the user's; 'as they ran' mixes the lane's exits with the entry. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_direction_seen_study.py"""
import datetime
import functools
import json
import re
import statistics
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


def eff(T, V, a, b):
    """net move over total path for the 10 s samples between a and b; (efficiency, signed net) or None."""
    pts, last = [], None
    for t, v in zip(T, V):
        if a <= t <= b and (last is None or t - last >= 10.0):
            pts.append(v)
            last = t
    if len(pts) < 20:
        return None
    path = sum(abs(pts[i + 1] - pts[i]) for i in range(len(pts) - 1))
    return (abs(pts[-1] - pts[0]) / path if path > 0 else 0.0, pts[-1] - pts[0])


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
        ticks_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 8 * 3600.0, r["ts"])).fetchall()
        if len(ticks_) < 20000:
            continue
        T = [float(x[0]) for x in ticks_]
        V = [float(x[1]) for x in ticks_]
        now = r["ts"]
        cell = []
        for span in (900.0, 3600.0):
            cur = eff(T, V, now - span, now)
            hist = [e[0] for k in range(1, int((8 * 3600.0 - span) // span) + 1) if (e := eff(T, V, now - span * (k + 1), now - span * k))]
            if cur is None or len(hist) < 3:
                cell = None
                break
            med = statistics.median(hist)
            way = (cur[1] > 0) == (side == "yes")
            cell.append((cur[0] > med, way))
        if cell is None:
            continue
        out.append((tk, side, pay, cell[0], cell[1], net, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result and 8 hours of spot history")


def line(label, g):
    if not g:
        print(f"{label:62s} n   0")
        return
    tot = sum(x[5] * x[6] for x in g)
    lost = sum(1 for x in g if x[5] < 0)
    print(f"{label:62s} n {len(g):3d}  net ${tot:+8.2f}  lost {lost:3d} ({100.0 * lost / len(g):.0f}%)  per contract {sum(x[5] for x in g) / len(g):+.3f}")


line("all", out)
for nm, i in (("15 min", 3), ("hour", 4)):
    line(f"{nm}: directional and with the bet", [x for x in out if x[i][0] and x[i][1]])
    line(f"{nm}: directional and AGAINST the bet", [x for x in out if x[i][0] and not x[i][1]])
    line(f"{nm}: chop (efficiency under its own median)", [x for x in out if not x[i][0]])
both_ok = [x for x in out if x[3][0] and x[3][1] and x[4][0] and x[4][1]]
line("BOTH windows directional and with the bet", both_ok)
one = [x for x in out if (x[3][0] and x[3][1]) or (x[4][0] and x[4][1])]
line("EITHER window directional and with the bet (buy; else hold)", one)
line("held back by 'either': neither window directional with the bet", [x for x in out if x not in one])
print("\nthe 22:00 and 22:15 bets (D = directional, c = chop; + with the bet, - against):")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0]:
        f = lambda c: ("D" if c[0] else "c") + ("+" if c[1] else "-")
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} 15min {f(x[3])} hour {f(x[4])} net {x[5]:+.3f}")
