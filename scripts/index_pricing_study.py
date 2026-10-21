"""Would pricing crypto 15-minute windows from Kalshi's own settlement index beat the market's price?

The tape (data/tape.db) holds Kalshi's streamed index (BRTI, ETHUSD_RTI, ...) with its trailing 60-second average, and
every quote change of each 15-minute market. A window settles on the index's 60-second average at the close compared with
the strike, so there is no feed-to-Kalshi offset to learn. This script:

  1. checks the settlement mechanics: does the 60-second average at the close reproduce each window's result?
  2. prices each window from the index alone at a few moments before the close (lognormal; the spread of the final
     60-second average is included) and compares that with the market's mid price, by Brier score;
  3. buys whichever side the index model likes by at least `edge` over the ask and reports the return after fees.

No orders are placed.    python scripts/index_pricing_study.py tape.db moonshot.db
"""
import bisect, math, random, sqlite3, sys
from statistics import NormalDist
from kalshi_moonshot.strategy import fee_per_contract as fee

N = NormalDist()
INDEX = {"KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI"}
tape = sqlite3.connect(sys.argv[1]); main = sqlite3.connect(sys.argv[2]); main.row_factory = sqlite3.Row

ticks = {}
for iid in INDEX.values():
    rows = tape.execute("select ts, value, avg60 from index_ticks where index_id=? order by ts", (iid,)).fetchall()
    ticks[iid] = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
lo = max(t[0][0] for t in ticks.values() if t[0]); hi = min(t[0][-1] for t in ticks.values() if t[0])


def at(iid, when, col=1):
    ts, vals, avgs = ticks[iid]
    i = bisect.bisect_right(ts, when) - 1
    if i < 0 or when - ts[i] > 5:                     # no tick within 5 seconds
        return None
    return (vals if col == 1 else avgs)[i]


def vol_per_minute(iid, when, minutes=60):
    closes = [at(iid, when - 60 * k) for k in range(minutes + 1)]
    rets = [math.log(a / b) for a, b in zip(closes, closes[1:]) if a and b]
    return math.sqrt(sum(r * r for r in rets) / len(rets)) if len(rets) >= 30 else None


quotes_by = {}
def mid_ask_bid(ticker, when):
    if ticker not in quotes_by:
        rows = tape.execute("select ts, bid, ask from quotes where ticker=? order by ts", (ticker,)).fetchall()
        quotes_by[ticker] = ([r[0] for r in rows], rows)
    ts, rows = quotes_by[ticker]
    i = bisect.bisect_right(ts, when) - 1
    if i < 0 or when - ts[i] > 120:
        return None
    _, bid, ask = rows[i]
    return (bid, ask) if bid is not None and ask is not None and 0 < ask <= 1 else None


wins = []
for series, iid in INDEX.items():
    for w in main.execute("select ticker, close_ts, strike, result from windows where series=? and result in ('yes','no') and strike is not null", (series,)):
        if w["close_ts"] - 900 - 3600 >= lo and w["close_ts"] <= hi:
            wins.append(dict(series=series, iid=iid, **dict(w)))
print(f"{len(wins)} settled crypto windows inside the tape ({(hi - lo) / 3600:.1f} hours)\n")

# 1. settlement mechanics
ok = tot = 0
close_gap = []
for w in wins:
    a = at(w["iid"], w["close_ts"], 2)
    s0 = at(w["iid"], w["close_ts"] - 900, 2)
    if a is None:
        continue
    tot += 1
    ok += (a > w["strike"]) == (w["result"] == "yes")
    if s0:
        close_gap.append(abs(math.log(s0 / w["strike"])))
close_gap.sort()
print(f"settlement check: the index's 60-second average at the close reproduces the result in {ok}/{tot} windows ({ok / tot:.1%});")
print(f"the strike sits within {close_gap[len(close_gap) // 2]:.5%} (median) of that average at the window's open\n")

# 2./3. pricing
rows = []
for w in wins:
    for left in (6, 5, 4, 3, 2):
        t = w["close_ts"] - left * 60
        spot = at(w["iid"], t); s1 = vol_per_minute(w["iid"], t); q = mid_ask_bid(w["ticker"], t)
        if not (spot and s1 and q):
            continue
        for vm in (1.0, 1.25):
            var = (s1 * vm) ** 2 * (max(left - 1, 0) + 1 / 3)              # time to the averaging window plus its own spread
            p = N.cdf(math.log(spot / w["strike"]) / math.sqrt(var))
            rows.append(dict(w=w, left=left, vm=vm, p=min(max(p, 0.005), 0.995), bid=q[0], ask=q[1], yes=w["result"] == "yes"))


def brier(items, f):
    return sum((f(r) - r["yes"]) ** 2 for r in items) / len(items)


print(f"{'minutes left':>12} {'windows':>8} {'index model':>12} {'market mid':>11}   (Brier, lower is better; vol x1.25)")
for left in (6, 5, 4, 3, 2):
    g = [r for r in rows if r["left"] == left and r["vm"] == 1.25]
    if g:
        print(f"{left:>12} {len(g):>8} {brier(g, lambda r: r['p']):>12.4f} {brier(g, lambda r: (r['bid'] + r['ask']) / 2):>11.4f}")


def trades(left_set, vm, edge, lo_p=0.0, hi_p=1.0):
    out = []
    for r in rows:
        if r["left"] not in left_set or r["vm"] != vm:
            continue
        for side, price, chance in (("yes", r["ask"], r["p"]), ("no", 1 - r["bid"], 1 - r["p"])):
            if lo_p <= price <= hi_p and chance - price - fee(price) >= edge:
                out.append((r["w"]["ticker"], price, r["yes"] == (side == "yes")))
    return out


def roi(items):
    cost = sum(p + fee(p) for _, p, _ in items)
    return sum(w for *_, w in items) / cost - 1 if cost else float("nan")


random.seed(5)
print(f"\n{'rule (entry with 2-6 min left)':44} {'n':>5} {'won':>5} {'paid':>6} {'ROI':>8}   90% range")
for vm in (1.0, 1.25):
    for edge in (0.0, 0.03, 0.06, 0.10):
        for label, (a, b) in (("any price", (0.0, 1.0)), ("70-95c", (0.70, 0.95)), ("under 40c", (0.0, 0.40))):
            it = trades({6, 5, 4, 3, 2}, vm, edge, a, b)
            if len(it) < 30:
                continue
            by = {}
            for x in it:
                by.setdefault(x[0], []).append(x)
            ks = list(by)
            bs = sorted(roi([y for k in random.choices(ks, k=len(ks)) for y in by[k]]) for _ in range(200))
            print(f"vol x{vm:<4} edge>={edge:.2f} {label:22} {len(it):>5} {sum(w for *_, w in it) / len(it):>5.0%} "
                  f"{sum(p for _, p, _ in it) / len(it):>6.2f} {roi(it):>+8.1%}   [{bs[10]:+.0%}, {bs[190]:+.0%}]")
