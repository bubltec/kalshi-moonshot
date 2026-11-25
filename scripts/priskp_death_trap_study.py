"""What marks a death trap at the buy? (2026-11-24; user: "we need to figure out how to prevent the buys from buying into a death trap"; "I wouldn't buy into this", on ZEC 23:00 NO and the 22:15 NO bets, with 1H charts showing the spot crossing the target again and again.)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, how many times the spot crossed this market's own target (its strike) in the hour before the buy (10 s samples, no dead band), and is a death trap (a bet that ended at -0.40 a contract or worse as the lane ran it) more likely after more crossings? Also the crossings per quartile, and the 22:15, ZEC and 22:00 bets listed.
HONEST LIMITS: one day (106 bets) in one regime; 'death trap' = -0.40 or worse after both fees, a line I chose before the result; the reading is chosen from the user's charts, not from this data, and no other reading is tried here (earlier studies of drift, range position and direction found no clean separation); the quartile cuts are the data's own; 'as run' mixes the lane's exits with the entry. A lead only; a rule on it goes to shadow first.
Run on the host: scripts/cloud.sh run scripts/priskp_death_trap_study.py"""
import datetime
import functools
import json
import re
import traceback
import urllib.request

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series

print = functools.partial(print, flush=True)
TRAP = -0.40
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
        m = market(tk)
        res, strike = m.get("result"), float(m.get("floor_strike") or 0)
        if not res or strike <= 0:
            continue
        net = (float(r["exit_price"]) - fee(float(r["exit_price"])) - cost) if r["exit_ts"] else ((1.0 if res == side else 0.0) - cost)
        ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", tk).group(1))
        ticks_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 3600.0, r["ts"])).fetchall()
        pts, last = [], None
        for t, v in ticks_:
            if last is None or t - last >= 10.0:
                pts.append(float(v))
                last = t
        if len(pts) < 300:
            continue
        above = [p > strike for p in pts]
        cross = sum(1 for i in range(len(above) - 1) if above[i] != above[i + 1])
        out.append((tk, side, pay, cross, net, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result and an hour of spot history")
traps = [x for x in out if x[4] <= TRAP]
print(f"death traps (net {TRAP:+.2f} or worse): {len(traps)}; their crossings median {sorted(x[3] for x in traps)[len(traps) // 2] if traps else 0}, the others' median {sorted(x[3] for x in out if x[4] > TRAP)[len([x for x in out if x[4] > TRAP]) // 2]}")


def line(label, g):
    if not g:
        print(f"{label:44s} n   0")
        return
    t = sum(x[4] * x[5] for x in g)
    tr = sum(1 for x in g if x[4] <= TRAP)
    print(f"{label:44s} n {len(g):3d}  as run ${t:+8.2f}  traps {tr:3d} ({100.0 * tr / len(g):.0f}%)  per contract {sum(x[4] for x in g) / len(g):+.3f}")


line("all", out)
srt = sorted(out, key=lambda x: x[3])
q = len(srt) // 4
for k in range(4):
    g = srt[k * q:(k + 1) * q if k < 3 else len(srt)]
    line(f"crossings quartile {k + 1} ({g[0][3]}-{g[-1][3]})", g)
print("\nthe 22:00, 22:15 and ZEC 23:00 bets:")
for x in sorted(out, key=lambda x: x[0]):
    if "2200" in x[0] or "2215" in x[0] or "KXZEC15M-26NOV242300" in x[0]:
        print(f"{x[0][:24]:24s} {x[1]:3s} paid {x[2]:.2f} crossings in the hour {x[3]:3d} net {x[4]:+.3f}{'  TRAP' if x[4] <= TRAP else ''}")
