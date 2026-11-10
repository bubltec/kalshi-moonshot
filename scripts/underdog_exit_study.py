"""What can an exit do for an underdog that turns out to be a bad market? (2026-11-10, user: "if we buy a bad market, maybe we have a way to prevent too much loss or even gain a tiny profit instead"; the live case: KXBTC15M-26NOV101200 YES bought at 0.33, the bid reached 0.36 within 50 s, then bled to 0.)
Question: on the same underdog-priced tape states as scripts/underdog_signs_study.py (each side's first 0.20-0.40 ask 30 s-7.5 min into a settled 15-minute crypto market since 10-29, bought at the ask plus the buy fee), does selling on a bid that has run up (take profit) or fallen (stop) beat holding to the result, after the sell fee?
Units (rule 1): u = the bid's own typical minute = the rms one-second move of the held side's bid (forward-filled) from the window open to the entry, times sqrt(60); at least 25 s of history, else the state is dropped. Nothing is in cents.
Rules (grid fixed before the results; the choice is made after seeing them and is said to be so): take profit at the first second the bid net of the sell fee is at least the cost paid (ask plus buy fee) plus TP x u, TP = 0.0 (any net profit) / 0.5 / 1 / 2 / 3; stop at the first second the bid is at or under the ask paid minus SL x u, SL = none / 1 / 2 / 3. A sale is at the bid two seconds later; nothing from the last 8 s. Both together: whichever comes first. Held to the result is the baseline.
Subsets: ALL states, and GATED = the states the live hour-drift gate would take (the hour's drift against the bet at or under the sample's 25th percentile; the cutoff is from the whole sample, a live one comes from each market's own hours).
Also: how often the bid reaches the cost plus a tiny net profit at all, by price third (the "tiny profit" question).
Per variant: net per $ and the change against holding, bets sold, share sold at a profit, the two halves of the period, price thirds. A variant counts only when positive against holding in both halves and in each price third it claims.

    scripts/cloud_job.sh scripts/underdog_exit_study.py
"""

import array
import calendar
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", calendar.timegm((2026, 10, 29, 0, 0, 0))))
NOW = float(os.environ.get("UNTIL", time.time()))
NAN = float("nan")

COINS = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "DOGE": "KXDOGE15M", "BNB": "KXBNB15M", "ZEC": "KXZEC15M", "HYPE": "KXHYPE15M", "NEAR": "KXNEAR15M"}
IDX = {c: variants.crypto_index(ser) for c, ser in COINS.items()}
IDX = {c: i for c, i in IDX.items() if i}
PERP = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

wins = st.db.execute(
    "SELECT ticker,close_ts,strike,result FROM windows WHERE result IN ('yes','no') AND close_ts>? AND close_ts<? AND ticker LIKE 'KX%15M-%' ORDER BY close_ts", (SINCE + 600, NOW)
).fetchall()
wins = [w for w in wins if w["ticker"].split("-")[0] in COINS.values() and w["strike"]]
T0 = int(SINCE) - 3700
T1 = int(max(float(w["close_ts"]) for w in wins)) + 5
N = T1 - T0 + 1
print(f"markets {len(wins)} | coins {sorted(IDX)} | seconds {N}", flush=True)


def load_index(ix):
    g = array.array("d", [NAN]) * N
    day = 86400
    a = T0
    while a < T1:
        b = min(a + day, T1 + 1)
        for ts, v in tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<? AND value>0 ORDER BY ts", (ix, a, b)).fetchall():
            g[int(ts) - T0] = math.log(float(v))
        a = b
    last = NAN
    for k in range(N):
        if g[k] == g[k]:
            last = g[k]
        else:
            g[k] = last
    sq, ab = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(1, N):
        d = g[k] - g[k - 1]
        d = d if d == d else 0.0
        sq[k + 1] = sq[k] + d * d
        ab[k + 1] = ab[k] + abs(d)
    return g, sq, ab


G = {c: load_index(i) for c, i in IDX.items() if c in COINS}
print("indexes loaded", flush=True)




def unit(c, k, w=900):
    sq = G[c][1]
    v = (sq[k + 1] - sq[k + 1 - w]) / w
    return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None


TPS = (0.0, 0.5, 1.0, 2.0, 3.0)
SLS = (None, 1.0, 2.0, 3.0)
DELAY, LAST = 2, 8
states = []
for wi, w in enumerate(wins):
    tk, close = w["ticker"], float(w["close_ts"])
    coin = next(c for c, ser in COINS.items() if ser == tk.split("-")[0])
    if coin not in G:
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, close - 900, close)).fetchall()
    taken = set()
    qt = [float(x[0]) for x in q]
    for idx_q, (ts, bid, ask) in enumerate(q):
        ts = float(ts)
        if not (close - 900 + 30 <= ts <= close - 900 + 450):
            continue
        yb, ya = float(bid), float(ask)
        if ya - yb > 0.06:
            continue
        for side, a in (("yes", ya), ("no", 1.0 - yb)):
            if side in taken or not (0.20 <= a <= 0.40):
                continue
            taken.add(side)
            k = int(ts) - T0
            if k < 3700 or k >= N - 5:
                continue
            sign = 1.0 if side == "yes" else -1.0
            # the held side's bid on a one-second grid from the window open to the close
            open_t, end_t = int(close - 900), int(close)
            bids = []
            j = 0
            last = None
            for t in range(open_t, end_t + 1):
                while j + 1 < len(qt) and qt[j + 1] <= t:
                    j += 1
                if qt[j] <= t and t - qt[j] <= 20:
                    last = float(q[j][1]) if side == "yes" else 1.0 - float(q[j][2])
                else:
                    last = None
                bids.append(last)
            e = int(ts) - open_t
            seq = [b for b in bids[: e + 1] if b is not None]
            moves = [(y - x) ** 2 for x, y in itertools.pairwise(seq)]
            if len(moves) < 25:
                continue
            u = math.sqrt(sum(moves) / len(moves)) * math.sqrt(60.0)
            if u <= 0:
                continue
            ul = unit(coin, k, 3600)
            if not ul:
                continue
            hour = (G[coin][0][k] - G[coin][0][k - 3600]) * sign / ul
            states.append(dict(ts=ts, ask=a, side=side, win=w["result"] == side, e=e, bids=bids, u=u, hour=hour))
    if wi % 1000 == 0:
        print(f"  {wi} markets, {len(states)} states", flush=True)
print(f"\nstates {len(states)} | win {sum(s_['win'] for s_ in states) / len(states):.3f} | mean ask {sum(s_['ask'] for s_ in states) / len(states):.3f}", flush=True)

hours = sorted(s_["hour"] for s_ in states)
hcut = hours[len(hours) // 4]
mid = sorted(s_["ts"] for s_ in states)[len(states) // 2]
asks = sorted(s_["ask"] for s_ in states)
a1, a2 = asks[len(asks) // 3], asks[2 * len(asks) // 3]


def third(a):
    return "low" if a < a1 else "mid" if a < a2 else "high"  # ties go up: the asks pile up at the top of the band


def run(st_, tp, sl):
    """(net return per $ of the cost, sold, sold_profit) for one state under (tp, sl)."""
    a = st_["ask"]
    cost = a + fee(a)
    u, e, bids = st_["u"], st_["e"], st_["bids"]
    last_i = len(bids) - 1 - LAST
    for i in range(e + 1, last_i):
        b = bids[i]
        if b is None:
            continue
        hit_tp = tp is not None and b - fee(b) >= cost + tp * u
        hit_sl = sl is not None and b <= a - sl * u
        if hit_tp or hit_sl:
            s_ = bids[i + DELAY] if i + DELAY < len(bids) else None
            if s_ is None or s_ <= 0.0:
                continue
            got = s_ - fee(s_)
            return (got - cost) / cost, 1, 1 if got > cost else 0
    return ((1.0 if st_["win"] else 0.0) - cost) / cost, 0, 0


def report(name, rows):
    n = len(rows)
    base = [((1.0 if r["win"] else 0.0) - (r["ask"] + fee(r["ask"]))) / (r["ask"] + fee(r["ask"])) for r in rows]
    print(f"\n=== {name}: {n} states | held net per $ {sum(base) / n:+.4f}", flush=True)
    for pname in ("low", "mid", "high"):
        sub = [x for r, x in zip(rows, base) if third(r["ask"]) == pname]
        print(f"  held, {pname} ask third: n={len(sub)} net/$ {sum(sub) / max(len(sub), 1):+.4f}")
    out = []
    for tp in (None, *TPS):
        for sl in SLS:
            if tp is None and sl is None:
                continue
            res = [run(r, tp, sl) for r in rows]
            d = [x[0] - b for x, b in zip(res, base)]
            h1 = [x for x, r in zip(d, rows) if r["ts"] <= mid]
            h2 = [x for x, r in zip(d, rows) if r["ts"] > mid]
            by = {p: sum(x for x, r in zip(d, rows) if third(r["ask"]) == p) / max(sum(1 for r in rows if third(r["ask"]) == p), 1) for p in ("low", "mid", "high")}
            out.append((sum(d) / n, tp, sl, sum(x[1] for x in res), sum(x[2] for x in res), sum(h1) / max(len(h1), 1), sum(h2) / max(len(h2), 1), sum(x[0] for x in res) / n, by))
    out.sort(key=lambda o: -o[0])
    print("  (take-profit TP x u, stop SL x u): change in net per $ against holding, best 10 then worst 3")
    for o in out[:10] + out[-3:]:
        tp = "none" if o[1] is None else f"{o[1]:g}"
        sl = "none" if o[2] is None else f"{o[2]:g}"
        print(f"    TP {tp:>4} SL {sl:>4}: {o[0]:+.4f} (net {o[7]:+.4f}) sold {o[3]} ({o[4]} at a profit) | halves {o[5]:+.4f} / {o[6]:+.4f} | by price third low {o[8]['low']:+.4f} mid {o[8]['mid']:+.4f} high {o[8]['high']:+.4f}")
    both = [o for o in out if o[5] > 0 and o[6] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)}")


reach = {}
for r in states:
    cost = r["ask"] + fee(r["ask"])
    best = max((b - fee(b) for b in r["bids"][r["e"] + 1 : len(r["bids"]) - LAST] if b is not None), default=0.0)
    for m in (0.0, 0.01, 0.02, 0.03, 0.05):
        for key in (("all", third(r["ask"]), m), ("lost", third(r["ask"]), m)) if not r["win"] else (("all", third(r["ask"]), m),):
            d = reach.setdefault(key, [0, 0])
            d[0] += 1
            d[1] += 1 if best >= cost + m else 0
print(f"price thirds of the ask: low < {a1:.2f}, mid < {a2:.2f}, high above")
print("how often the bid, net of the sell fee, ever reaches the cost paid plus a margin after entry (all states / only the states that went on to lose):")
for grp in ("all", "lost"):
    for p in ("low", "mid", "high"):
        cells = []
        for m in (0.0, 0.01, 0.02, 0.03, 0.05):
            d = reach.get((grp, p, m))
            cells.append(f"+{m:.2f}: {d[1] / d[0]:.0%}" if d and d[0] else f"+{m:.2f}: n/a")
        n_ = reach.get((grp, p, 0.0), [0])[0]
        print(f"  {grp:4s} {p:5s} (n={n_}) " + " | ".join(cells))
report("ALL underdog states", states)
report("GATED (hour drift against the bet, at or under the sample's 25th percentile)", [r for r in states if r["hour"] <= hcut])
print("DONE")
