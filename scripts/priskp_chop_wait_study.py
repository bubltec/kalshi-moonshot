"""Should the wait depend on choppiness? (2026-11-24; user, after the settle_hold went live at a fixed 0.13 of the market: "What if choppiness impacts wait".)
QUESTION (one): on every live priskp bet since 2026-11-24 05:00 UTC, buy the same side at its ask after a WAIT, only if the ask is still in the lane's band 0.55-0.85 and the spot has not moved against the bet since the real buy; held to the market's own result, fee on the buy. WAIT is (a) fixed 117 s, (b) 117 s x chop ratio, (c) 117 s / chop ratio. Chop ratio = the previous 15 minutes' chop (1 - path efficiency, 10 s samples) over the median chop of the four 15-minute windows before it, limited to 0.5-2 (a bound I chose: a very quiet or very wild hour would otherwise give a wait of nothing or all the market). Also the chop ratio's own split: do the buys made in a choppier-than-usual market do worse?
HONEST LIMITS: held to settlement (the lane's exits are out of it); the other priskp checks are not re-run at the later time; one day (106 bets); the 0.5-2 limits and the four-window history are mine, the hour is what the tick memory holds; 'moved against' is the spot's sign over the wait, as in the earlier study. A lead only; the live step is not changed by this.
Run on the host: scripts/cloud.sh run scripts/priskp_chop_wait_study.py"""
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
BASE, LO, HI = 117.0, 0.55, 0.85
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


def market(t):
    with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=20) as r:
        return json.load(r)["market"]


def chop(T, V, a, b):
    pts, last = [], None
    for t, v in zip(T, V):
        if a <= t <= b and (last is None or t - last >= 10.0):
            pts.append(v)
            last = t
    if len(pts) < 20:
        return None
    path = sum(abs(pts[i + 1] - pts[i]) for i in range(len(pts) - 1))
    return 1.0 - (abs(pts[-1] - pts[0]) / path if path > 0 else 0.0)


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
        ticks_ = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 4500.0, r["ts"] + 700.0)).fetchall()
        T = [float(x[0]) for x in ticks_]
        V = [float(x[1]) for x in ticks_]
        now = r["ts"]
        cur = chop(T, V, now - 900.0, now)
        hist = [c for k in range(1, 5) if (c := chop(T, V, now - 900.0 * (k + 1), now - 900.0 * k)) is not None]
        if cur is None or len(hist) < 3:
            continue
        med = statistics.median(hist)
        ratio = min(2.0, max(0.5, cur / med)) if med > 0 else 1.0
        i0 = max(k for k, t in enumerate(T) if t <= now)
        res_by = {}
        for name, wait in (("fixed", BASE), ("x chop", BASE * ratio), ("/ chop", BASE / ratio)):
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk, now + wait, now + wait + 20.0)).fetchall()
            close = (int(now) // 900 + 1) * 900.0
            if not q or now + wait >= close - 60.0:
                res_by[name] = None
                continue
            a = float(q[0]["ask"]) if side == "yes" else 1.0 - float(q[0]["bid"])
            if not (LO <= a <= HI):
                res_by[name] = None
                continue
            later = [v for t, v in zip(T, V) if t <= now + wait]
            v1 = later[-1]
            ok = (v1 >= V[i0]) if side == "yes" else (v1 <= V[i0])
            res_by[name] = (win - a - fee(a)) if ok else None
        out.append((tk, side, ratio, real, res_by, n))
    except Exception:
        print(r["ticker"], "failed", traceback.format_exc()[-200:])
print(f"{len(out)} bets with a result and a chop reading")


def tot(g, f):
    return sum(f(x) * x[5] for x in g)


print(f"real buys held to the result: ${tot(out, lambda x: x[3]):+.2f} on {len(out)}")
for name in ("fixed", "x chop", "/ chop"):
    g = [x for x in out if x[4][name] is not None]
    print(f"  wait {name:7s}: buys only if the spot is not against the bet after the wait: {len(g):3d} bets, ${tot(g, lambda x: x[4][name]):+.2f}  (the same bets bought for real ${tot(g, lambda x: x[3]):+.2f})")
srt = sorted(out, key=lambda x: x[2])
half = len(srt) // 2
for label, g in (("calmer than usual (lower half of the chop ratio)", srt[:half]), ("choppier than usual (upper half)", srt[half:])):
    lost = sum(1 for x in g if x[3] < 0)
    print(f"real buys, {label}: n {len(g)} held ${tot(g, lambda x: x[3]):+.2f} lost {lost} ({100.0 * lost / len(g):.0f}%)")
