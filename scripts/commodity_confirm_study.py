"""settle_confirm as a BUY adjustment on the commodity lanes (2026-11-24; user, on the commodity exit: "settle_confirm on the priskp might be interesting too as a buy adjustment instead").
QUESTION (one): on every live commodity bet since 2026-11-18 20:00 UTC (every commodity lane but multiscale, mode trade, one per market, side and lane, with a settled result), treat the real buy as the first moment the lane wanted to buy, note the Kalshi perp reference then (KP_* index), and buy the same side at its ask at the FIRST moment after it when the reference has moved the bet's way by at least ONE typical minute of its own (priskp.typical_minute over the hour before, exactly the buy step's reading, no clock). Not bought if that never happens before 60 s before the close. Held to the market's own result, fee on the buy only, as the priskp study did.
Compared with the same bets bought for real, held to the result. Also the result by the bid's path efficiency before the buy in thirds (proxy for curve shape), and the confirmed price against the real one.
HONEST LIMITS: held to settlement (the lane's exits are out of it); the lane's other buy steps are not re-run at the later time (price band, cushion); one regime, about six days; ONE typical minute is the one setting run. A lead only.
    scripts/cloud_job.sh scripts/commodity_confirm_study.py
"""
import bisect
import calendar
import math
import os

from kalshi_moonshot import config, db, perpref, priskp
from kalshi_moonshot.store import Store

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 18, 20, 0, 0))
UNITS = 1.0


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.db.execute(
    "SELECT ts,ticker,lane,side,ask,stake_usd,outcome,close_time FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND outcome IS NOT NULL ORDER BY ts",
    (t0,),
).fetchall()
seen = {}
for r in rows:
    seen[(r["ticker"], r["lane"], r["side"], round(r["ask"], 3))] = r
print("commodity live bets with a result since 2026-11-18 20:00 UTC:", len(seen))


def close_of(r):
    try:
        return float(r["close_time"])
    except Exception:
        pass
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return r["ts"] + 450.0


out, skipped = [], 0
for r in seen.values():
    series = r["ticker"].split("-")[0]
    ix = perpref.index_for_series(series)
    if not ix:
        skipped += 1
        continue
    close = close_of(r)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, r["ts"] - 3600.0, close)).fetchall()
    T, V = [float(x[0]) for x in tk], [float(x[1]) for x in tk]
    pre = bisect.bisect_right(T, r["ts"])
    if pre < 60:
        skipped += 1
        continue
    unit = priskp.typical_minute(T[:pre], V[:pre], r["ts"], 3600.0)
    if unit <= 0:
        skipped += 1
        continue
    v0 = V[pre - 1]
    need = UNITS * unit
    sign = 1.0 if r["side"] == "yes" else -1.0
    when = None
    for t, v in zip(T[pre:], V[pre:]):
        if t >= close - 60.0:
            break
        if sign * (v - v0) >= need:
            when = t
            break
    win = 1.0 if r["outcome"] == r["side"] else 0.0
    real = win - r["ask"] - fee(r["ask"])
    conf, ask2 = None, None
    if when:
        q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], when, when + 20.0)).fetchall()
        if q:
            ask2 = float(q[0]["ask"]) if r["side"] == "yes" else 1.0 - float(q[0]["bid"])
            if 0.02 < ask2 < 0.99:
                conf = win - ask2 - fee(ask2)
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"] - 300.0, r["ts"])).fetchall()
    sb = [float(x["bid"]) if r["side"] == "yes" else 1.0 - float(x["ask"]) for x in q]
    path = sum(abs(b - a) for a, b in zip(sb, sb[1:]))
    eff = abs(sb[-1] - sb[0]) / path if len(sb) > 2 and path > 0 else 0.0
    out.append({"tk": r["ticker"], "lane": r["lane"], "n": r["stake_usd"] / r["ask"], "real": real, "conf": conf, "wait": (when - r["ts"]) if when else None, "ask": r["ask"], "ask2": ask2, "eff": eff, "win": win})
print(f"bets replayed: {len(out)}  skipped (no index ticks or unit): {skipped}")


def block(label, sub):
    if not sub:
        return
    c = [x for x in sub if x["conf"] is not None]
    nb = [x for x in sub if x["conf"] is None]
    real_all = sum(x["real"] * x["n"] for x in sub)
    real_c = sum(x["real"] * x["n"] for x in c)
    conf_c = sum(x["conf"] * x["n"] for x in c)
    real_nb = sum(x["real"] * x["n"] for x in nb)
    waits = sorted(x["wait"] for x in c)
    wins_c = sum(x["win"] for x in c)
    wins_nb = sum(x["win"] for x in nb)
    print(f"\n{label}: n={len(sub)}  real held ${real_all:+.2f}")
    print(f"  confirmed and bought {len(c)}: confirmation ${conf_c:+.2f} vs the same bets for real ${real_c:+.2f} (change ${conf_c - real_c:+.2f}); won {wins_c:.0f} of {len(c)}; median wait {waits[len(waits) // 2]:.0f} s" if c else "  none confirmed")
    print(f"  never confirmed (the rule skips them) {len(nb)}: real ${real_nb:+.2f}, won {wins_nb:.0f} of {len(nb)}")
    if c:
        print(f"  mean ask: real {sum(x['ask'] for x in c) / len(c):.3f}, at confirmation {sum(x['ask2'] for x in c) / len(c):.3f}")


block("ALL", out)
effs = sorted(x["eff"] for x in out)
if len(effs) >= 9:
    c1, c2 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    block(f"path efficiency, low third (<= {c1:.2f})", [x for x in out if x["eff"] <= c1])
    block(f"middle third ({c1:.2f} to {c2:.2f})", [x for x in out if c1 < x["eff"] <= c2])
    block(f"high third (> {c2:.2f})", [x for x in out if x["eff"] > c2])
for ln in sorted({x["lane"] for x in out}):
    block(ln, [x for x in out if x["lane"] == ln])
