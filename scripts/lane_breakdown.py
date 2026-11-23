"""One lane's settled bets, cut along the things its settings control. (2026-11-12, user: "leave crypto_multiscale on, and lets look at what we can adjust there before we push")
No rule and no model. LANE (default crypto_multiscale) since SINCE (default 2026-11-05) from `decisions`, real and paper apart (mode is a label). Each cut reports bets, dollars staked, as traded (the real exit, fees in), held to settlement, how many made and lost money, and the mean ask:
  by version of the lane's settings (with the first and last buy of each, so a change of settings can be read against its result),
  and, for the bets since Monday 2026-11-09 only: by the price paid, by the minute of the window at the buy, by the recorded edge at the buy (thirds), by how the bet ended, by day.
A cut here is a description of what happened under settings that changed during the week, not a tested rule: a setting picked from it is chosen after seeing the result and has to be scored forward.
    scripts/cloud_job.sh scripts/lane_breakdown.py LANE=crypto_multiscale
"""

import collections
import datetime as dt
import os
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = ZoneInfo("America/New_York")
LANE = os.environ.get("LANE", "crypto_multiscale")
d0 = dt.date.fromisoformat(os.environ.get("SINCE", "2026-11-05"))
T0 = dt.datetime(d0.year, d0.month, d0.day, tzinfo=ET).timestamp()
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
store = Store(s.db_path, s.exclude_versions)
closes = {w["ticker"]: float(w["close_ts"]) for w in store.db.execute("SELECT ticker,close_ts FROM windows WHERE close_ts>?", (T0 - 3600,)).fetchall()}
rows = store.db.execute("SELECT ts,mode,ticker,side,ask,edge,model_p,stake_usd,outcome,exit_price,exit_reason,version,reason FROM decisions WHERE action='bet' AND lane=? AND ts>? AND outcome IS NOT NULL AND stake_usd>0 AND ask>0 ORDER BY ts", (LANE, T0)).fetchall()
B = []
for r in rows:
    ask, stake, ts = float(r["ask"]), float(r["stake_usd"]), float(r["ts"])
    n = stake / (ask + fee(ask))
    won = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    xp = r["exit_price"]
    c = closes.get(r["ticker"])
    B.append({
        "mode": r["mode"], "ts": ts, "ask": ask, "stake": stake, "traded": n * ((float(xp) - fee(float(xp))) if xp is not None else won) - stake, "held": n * won - stake,
        "minute": None if c is None else 15 - (c - ts) / 60, "edge": r["edge"], "p": r["model_p"], "version": r["version"] or "-",
        "end": ((r["exit_reason"] or "sold").split(":")[0].strip() if xp is not None else ("won at settlement" if won else "lost at settlement")), "day": dt.datetime.fromtimestamp(ts, ET).strftime("%m-%d %a"), "reason": r["reason"] or "",
    })
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731


def line(label, xs):
    if not xs:
        return f"    {label:30s} n=0"
    return f"    {label:30s} n={len(xs):4d} | staked {sum(x['stake'] for x in xs):8.2f} | as traded {sum(x['traded'] for x in xs):+8.2f} | held {sum(x['held'] for x in xs):+8.2f} | made money {sum(1 for x in xs if x['traded'] > 0)}, lost {sum(1 for x in xs if x['traded'] < 0)} | mean ask {statistics.fmean(x['ask'] for x in xs):.2f}"


def bands(xs, key, cuts, fmt):
    for a, b in itertools_pairs(cuts):
        print(line(fmt.format(a, b), [x for x in xs if x[key] is not None and a <= x[key] < b]))
    miss = [x for x in xs if x[key] is None]
    if miss:
        print(line("not recorded", miss))


def itertools_pairs(c):
    return list(zip(c, c[1:], strict=False))


print(f"{LANE}: {len(B)} settled bets since {d0} ET; modes {dict(collections.Counter(x['mode'] for x in B))}")
for mode in sorted({x["mode"] for x in B}):
    M = [x for x in B if x["mode"] == mode]
    print(f"\n=== mode {mode} ===")
    print("  by version of the settings (label), in the order they ran")
    for v in sorted({x["version"] for x in M}, key=lambda v: min(x["ts"] for x in M if x["version"] == v)):
        xs = [x for x in M if x["version"] == v]
        print(line(f"{str(v)[:12]} {hm(xs[0]['ts'])}>{hm(xs[-1]['ts'])[6:]}", xs))
    W = [x for x in M if x["ts"] >= MON]
    print(f"  since Monday 11-09: {len(W)} bets")
    print(line("all", W))
    print("  by the price paid")
    bands(W, "ask", [0.0, 0.2, 0.4, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 1.01], "ask {:.2f}-{:.2f}")
    print("  by the minute of the window at the buy")
    bands(W, "minute", [0, 2, 4, 7, 9, 11, 13, 15.01], "minute {:.0f}-{:.0f}")
    e = sorted(x["edge"] for x in W if x["edge"] is not None)
    if len(e) > 30:
        c1, c2 = e[len(e) // 3], e[2 * len(e) // 3]
        print(f"  by the recorded edge at the buy (cuts {c1:+.3f}, {c2:+.3f})")
        print(line("edge low", [x for x in W if x["edge"] is not None and x["edge"] < c1]))
        print(line("edge mid", [x for x in W if x["edge"] is not None and c1 <= x["edge"] < c2]))
        print(line("edge high", [x for x in W if x["edge"] is not None and x["edge"] >= c2]))
    print("  by how the bet ended")
    for k, _ in collections.Counter(x["end"] for x in W).most_common():
        print(line(k[:30], [x for x in W if x["end"] == k]))
    print("  by day")
    for d in sorted({x["day"] for x in W}):
        print(line(d, [x for x in W if x["day"] == d]))
    print("  three recent buy reasons, as recorded")
    for x in W[-3:]:
        print("    " + x["reason"][:420])
print("DONE")
