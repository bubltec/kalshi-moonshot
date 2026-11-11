"""The buy side of the two multiscale lanes, bets at or over the lanes' current price floor (PRICE_LO, 0.55) (2026-11-11, user: "i want to focus on the multiscale lanes now. both crypto and commodity").
Question: on each lane's own settled live bets, HELD TO SETTLEMENT (the buy alone, no exit), which curve readings at the buy paid and which lost, and how much of the loss came from several same-direction bets in one window?
Per bet, read at the second of the buy from the tape: the curve's chance P (900 s horizon rate) and the price paid (stake / contracts, fee included), so edge = P - price; the cushion in typical minutes; and from the decision row: minutes elapsed, the curve model's score, the market type tag, the 60-minute trend against the bet's side. Splits are fixed before the results; nothing is keyed on a market name (rule 1). Each row: bets, won, staked, net held, net per dollar, and the two halves of the period by time.
Stacking: bets of one lane that share a window close and a side are one group; groups by size, and what a cap of 1 / 2 / 3 bets a group (the earliest kept) would have left.
    scripts/cloud_job.sh scripts/multiscale_buy_study.py
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
LO = float(os.environ.get("PRICE_LO", "0.55"))  # the lanes' current price floor: crypto_multiscale bought cheaper sides before the underdog lane was split out, and those bets are a different rule


def bucket(x, cuts, labels):
    for c, lab in zip(cuts, labels):
        if x < c:
            return lab
    return labels[-1]


def table(title, rows, key, order=None):
    groups = collections.defaultdict(list)
    for x in rows:
        k = key(x)
        if k is not None:
            groups[k].append(x)
    mid = sorted(x["ts"] for x in rows)[len(rows) // 2]
    print(f"  {title}")
    keys = order if order else sorted(groups)
    for k in keys:
        g = groups.get(k, [])
        if not g:
            continue
        st_ = sum(x["stake"] for x in g)
        net = sum(x["held"] for x in g)
        h1 = sum(x["held"] for x in g if x["ts"] <= mid)
        print(f"    {str(k):24s} n {len(g):4d} | won {sum(x['won'] for x in g):4d} ({sum(x['won'] for x in g) / len(g):.0%}) | staked {st_:8.2f} | held {net:+8.2f} ({net / st_:+.1%} per $) | halves {h1:+.2f} / {net - h1:+.2f}")


for lane in LANES:
    q = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,d.reason,d.factors,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        "WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.lane=? AND w.result IN ('yes','no') ORDER BY d.ts",
        (lane,),
    ).fetchall()
    seen = {}
    for r in q:
        seen[(r["ticker"], r["side"])] = r
    rows = []
    for r in seen.values():
        got = prepare(r)
        n, cost = float(r["contracts"]), float(r["stake_usd"])
        won = 1 if r["result"] == r["side"] else 0
        x = {"ts": float(r["ts"]), "stake": cost, "held": n * won - cost, "won": won, "price": cost / n, "side": r["side"], "close": float(r["close_ts"]), "edge": None, "cush": None, "p": None}
        if got and got[1]:
            t, left, c, rates, bid = got[1][0]
            if rates[1] > 0 and rates[0] > 0 and left > 0:
                x["p"] = phi(c / (rates[1] * math.sqrt(left)))
                x["edge"] = x["p"] - x["price"]
                x["cush"] = c / (rates[0] * math.sqrt(60.0))
        reason = r["reason"] or ""
        m = re.search(r"curve model ([+-][0-9.]+) per", reason)
        x["score"] = float(m.group(1)) if m else None
        m = re.search(r"\[mtype ([a-z]+)\]", reason)
        x["mtype"] = m.group(1) if m else None
        m = re.search(r"\[trend60 ([+-][0-9.]+)%\]", reason)
        tr = float(m.group(1)) if m else None
        x["trend"] = None if tr is None else ("with the bet" if (tr > 0) == (r["side"] == "yes") and tr != 0 else "flat" if tr == 0 else "against the bet")
        try:
            f = json.loads(r["factors"]) if r["factors"] else {}
        except (TypeError, ValueError):
            f = {}
        x["elapsed"] = f.get("elapsed_min")
        rows.append(x)
    cheap = [x for x in rows if x["price"] < LO]
    print(f"\n--- {lane}: {len(rows)} settled bets in all; {len(cheap)} bought under {LO:.2f} (won {sum(x['won'] for x in cheap)}, held {sum(x['held'] for x in cheap):+.2f}) are left out below")
    rows = [x for x in rows if x["price"] >= LO]
    if len(rows) < 20:
        print("  too few bets")
        continue
    print(f"\n=== {lane}: {len(rows)} settled bets | won {sum(x['won'] for x in rows)} | staked {sum(x['stake'] for x in rows):.2f} | held {sum(x['held'] for x in rows):+.2f} | with a curve reading at the buy {sum(1 for x in rows if x['edge'] is not None)}")
    table("A. curve chance minus price paid", rows, lambda x: None if x["edge"] is None else bucket(x["edge"], (-0.10, -0.05, 0.0, 0.05, 0.10), ("under -0.10", "-0.10 to -0.05", "-0.05 to 0", "0 to +0.05", "+0.05 to +0.10", "+0.10 and up")), ("under -0.10", "-0.10 to -0.05", "-0.05 to 0", "0 to +0.05", "+0.05 to +0.10", "+0.10 and up"))
    table("B. cushion at the buy, typical minutes", rows, lambda x: None if x["cush"] is None else bucket(x["cush"], (0.0, 0.5, 1.0, 2.0), ("behind (under 0)", "0 to 0.5", "0.5 to 1", "1 to 2", "2 and up")), ("behind (under 0)", "0 to 0.5", "0.5 to 1", "1 to 2", "2 and up"))
    table("C. minutes elapsed at the buy", rows, lambda x: None if x["elapsed"] is None else bucket(float(x["elapsed"]), (4, 8, 11), ("under 4", "4 to 8", "8 to 11", "11 and up")), ("under 4", "4 to 8", "8 to 11", "11 and up"))
    table("D. price paid", rows, lambda x: bucket(x["price"], (0.60, 0.65, 0.70, 0.75), ("0.55 to 0.60", "0.60 to 0.65", "0.65 to 0.70", "0.70 to 0.75", "0.75 and up")), ("0.55 to 0.60", "0.60 to 0.65", "0.65 to 0.70", "0.70 to 0.75", "0.75 and up"))
    table("E. curve model score, per $1", rows, lambda x: None if x["score"] is None else bucket(x["score"], (0.03, 0.05, 0.08), ("under +0.03", "+0.03 to +0.05", "+0.05 to +0.08", "+0.08 and up")), ("under +0.03", "+0.03 to +0.05", "+0.05 to +0.08", "+0.08 and up"))
    table("F. market type tag", rows, lambda x: x["mtype"])
    table("G. 60-minute trend", rows, lambda x: x["trend"])
    table("H. side", rows, lambda x: x["side"])
    # stacking
    groups = collections.defaultdict(list)
    for x in rows:
        groups[(x["close"], x["side"])].append(x)
    size = {id(x): len(g) for g in groups.values() for x in g}
    table("I. same-direction bets in the same window (group size)", rows, lambda x: bucket(size[id(x)], (2, 3, 5), ("1 alone", "2", "3 to 4", "5 and up")), ("1 alone", "2", "3 to 4", "5 and up"))
    gl = sorted(groups.values(), key=lambda g: sum(x["held"] for x in g))
    print("    the five worst windows: " + "; ".join(f"{len(g)} bets {sum(x['held'] for x in g):+.2f}" for g in gl[:5]) + f" | the five best: " + "; ".join(f"{len(g)} bets {sum(x['held'] for x in g):+.2f}" for g in gl[-5:]))
    mid = sorted(x["ts"] for x in rows)[len(rows) // 2]
    for cap in (1, 2, 3):
        kept = [x for g in groups.values() for x in sorted(g, key=lambda y: y["ts"])[:cap]]
        net = sum(x["held"] for x in kept)
        h1 = sum(x["held"] for x in kept if x["ts"] <= mid)
        print(f"    cap of {cap} a window and side (the earliest kept): {len(kept)} bets | held {net:+.2f} on {sum(x['stake'] for x in kept):.2f} staked ({net / sum(x['stake'] for x in kept):+.1%} per $) | halves {h1:+.2f} / {net - h1:+.2f}")
    # the two readings together
    table("J. edge sign and cushion", rows, lambda x: None if x["edge"] is None else ("edge 0 or more" if x["edge"] >= 0 else "edge under 0") + (", cushion 1+" if x["cush"] >= 1 else ", cushion under 1"))
print("DONE")
