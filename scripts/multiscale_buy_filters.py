"""Two buy filters for the multiscale lanes, walk-forward by day (2026-11-11, user: yes, test them). From studies/results/2026-11-11-multiscale-buy.txt: bets whose curve chance was at or over the price paid did best, and bets bought with a flat 60-minute trend lost in both halves.
Question: on each lane's own settled live bets at or over the 0.55 price floor, held to settlement, would skipping (a) bets whose curve chance is under the price paid by more than a cutoff, or (b) bets with a flat 60-minute trend tag, have beaten buying them all, day by day?
Fixed rules (no tuning): edge >= -0.05; edge >= 0; trend not flat; and the pairs. Walk-forward rule: each UTC day the edge cutoff (none / -0.10 / -0.05 / -0.02 / 0 / +0.02) with the best held net on all earlier days is used that day (first day: none). Reported: per day and in total, bets kept, their held net and net per dollar, the skipped bets' held net, and on how many days the kept set beat buying all per dollar staked. "Flat" is the decision's own tag reading 0.0%, so it is a rounded reading, not a cutoff from the market's history (rule 2 would want that before this is a rule).
    scripts/cloud.sh run scripts/multiscale_buy_filters.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 300.0, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 120.0, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


import collections
import json
import re

LANES = ("crypto_multiscale", "commodity_multiscale")
LO = float(os.environ.get("PRICE_LO", "0.55"))
CUTS = (None, -0.10, -0.05, -0.02, 0.0, 0.02)


def summ(rows):
    st_ = sum(x["stake"] for x in rows)
    net = sum(x["held"] for x in rows)
    return len(rows), st_, net, (net / st_ if st_ else 0.0)


for lane in LANES:
    q = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,d.reason,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        "WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.lane=? AND w.result IN ('yes','no') ORDER BY d.ts",
        (lane,),
    ).fetchall()
    seen = {}
    for r in q:
        seen[(r["ticker"], r["side"])] = r
    rows = []
    for r in seen.values():
        n, cost = float(r["contracts"]), float(r["stake_usd"])
        if cost / n < LO:
            continue
        got = prepare(r)
        if not got or not got[1]:
            continue
        t, left, c, rates, bid = got[1][0]
        if rates[1] <= 0 or left <= 0:
            continue
        won = 1 if r["result"] == r["side"] else 0
        m = re.search(r"\[trend60 ([+-][0-9.]+)%\]", r["reason"] or "")
        rows.append({"ts": float(r["ts"]), "day": int(float(r["ts"]) // 86400), "stake": cost, "held": n * won - cost, "edge": phi(c / (rates[1] * math.sqrt(left))) - cost / n, "flat": (float(m.group(1)) == 0.0) if m else None})
    rows.sort(key=lambda x: x["ts"])
    print(f"\n=== {lane}: {len(rows)} settled bets at {LO:.2f} or over with a curve reading | held {sum(x['held'] for x in rows):+.2f} on {sum(x['stake'] for x in rows):.2f} | flat-trend tag read on {sum(1 for x in rows if x['flat'] is not None)}")
    days = sorted({x["day"] for x in rows})
    RULES = [
        ("edge >= -0.05", lambda x: x["edge"] >= -0.05),
        ("edge >= 0", lambda x: x["edge"] >= 0.0),
        ("trend not flat", lambda x: x["flat"] is not True),
        ("edge >= -0.05 and not flat", lambda x: x["edge"] >= -0.05 and x["flat"] is not True),
        ("edge >= 0 and not flat", lambda x: x["edge"] >= 0.0 and x["flat"] is not True),
    ]
    alln, alls, allnet, allper = summ(rows)
    print(f"  buy all: {alln} bets, held {allnet:+.2f} ({allper:+.1%} per $)")
    for name, keep in RULES:
        K = [x for x in rows if keep(x)]
        S = [x for x in rows if not keep(x)]
        kn, ks, knet, kper = summ(K)
        beat = lost = 0
        parts = []
        for d in days:
            A = [x for x in rows if x["day"] == d]
            Kd = [x for x in A if keep(x)]
            an, as_, anet, aper = summ(A)
            dn, ds, dnet, dper = summ(Kd)
            if Kd and len(Kd) < len(A):
                beat += dper > aper
                lost += dper < aper
            parts.append(f"{dn}/{an} {dnet:+.1f} vs {anet:+.1f}")
        print(f"  {name:28s}: kept {kn} bets, held {knet:+.2f} ({kper:+.1%} per $) | skipped {len(S)} bets that made {sum(x['held'] for x in S):+.2f} | days the kept set beat buying all per $: {beat}, lost: {lost}")
        print(f"      by day (kept/all, kept net vs all net): " + "; ".join(parts))
    # walk-forward cutoff
    tot = []
    picks = []
    for di, d in enumerate(days):
        past = [x for x in rows if x["day"] < d]
        best = None
        if past:
            best = max(CUTS, key=lambda c: sum(x["held"] for x in past if c is None or x["edge"] >= c))
        today = [x for x in rows if x["day"] == d]
        kept = [x for x in today if best is None or x["edge"] >= best]
        tot += kept
        picks.append(f"{best} ({len(kept)}/{len(today)} {sum(x['held'] for x in kept):+.1f} vs {sum(x['held'] for x in today):+.1f})")
    kn, ks, knet, kper = summ(tot)
    print(f"  walk-forward edge cutoff: kept {kn} bets, held {knet:+.2f} ({kper:+.1%} per $) against buying all {allnet:+.2f} ({allper:+.1%})")
    print("      cutoff used each day: " + "; ".join(picks))
print("DONE")
