"""What is the best round trip in each market, and what does the curve look like at its buy and sell? (2026-11-12, user: "it is possible to win on every market"; yes to the best-round-trip study)
No model, no picks. Every settled 15-minute market since Monday 2026-11-09 (the series the model lanes trade), each side, every 30-second moment from minute 1 to 14 with the curvefeatures.py library (about 106 features, in the curve's own variance units).
Part 1, the ceiling. Per market and side, the best round trip in hindsight: buy at one moment's ask (fee), sell at a later moment's bid of at most 0.90 (less the 0.02 exit slip and the fee; a bid above that is the winner sold into settlement, the first full run's median sale was 0.97). Settlement is not a sale: buying the winner and holding is hindsight about the result (the first run found that was 71% of best trips), so it is shown once for context and left out. Net per $1 spent. The market's best is the better side. Reported: how many markets have a positive one, its size (median, quartiles), how many are real waves (above 10% and 25%), when in the 15 minutes the best buy and sell fall and at what price. This is hindsight; it says what is on offer, not what is earnable.
Part 2, what the curve shows at the right moments. For markets whose best round trip is above 10% and sells before settlement: each feature's rank at the best buy moment (and at the best sell moment) among the same market-side's other moments, 0 = lowest of the market's own readings, 1 = highest, 0.5 if the feature says nothing. Ranking inside the market removes the price level and the asset. Mean rank, t over markets, the mean rank in terciles of the 13-minute level at that moment (the curve shape; it must keep its side of 0.5 in all three), sorted by |t|; the count past |t| = 2 against the 5% expected by chance. A feature that marks the trough and the peak is one the bot can read live; a hindsight maximum picks noise too, so the rank against the market's own other moments is the honest test, and a rule built on it is scored forward.
    scripts/cloud_job.sh scripts/best_round_trip.py          # MAX_MARKETS=300 for a quick look
"""

import bisect, datetime as dt, itertools, math, os, statistics, time
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.curvefeatures import moment_features
from kalshi_moonshot.curvemodel import MOMENT_STEP
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = dt.timezone(dt.timedelta(hours=-5))
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
HORIZ = (60, 120, 180)
MAXM = int(os.environ.get("MAX_MARKETS", "0"))
SELL_CAP = 0.90  # a bid above this is the winner being sold into settlement, the hindsight trade again; the waves are what is left
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (MON,)).fetchall()
wins = [w for w in wins if w["series"] in series and w["series"] not in RECORD_ONLY]
if MAXM:
    wins = wins[-MAXM:]
now = time.time()
g0 = int(MON - 3 * 3600 - 300)
g1 = int(now) + 2

FEATS = {}  # (ticker, side) -> [(e, ask, bid, features)]
cur_ix, g, sq, ts, vs = None, [], [0.0], [], []
RES = {}
for w in wins:
    ser = w["series"]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        continue
    if ix != cur_ix:
        cur_ix = ix
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
        ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
        g, j, last = [], 0, None
        for t in range(g0, g1):
            while j < len(ts) and ts[j] <= t:
                last = vs[j]
                j += 1
            g.append(math.log(last) if last else None)
        sq = [0.0]
        for a, b in itertools.pairwise(g):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    close = float(w["close_ts"])
    opened = close - 900.0
    if crypto:
        strike_log = math.log(float(w["strike"]))
    else:
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        strike_log = math.log(vs[a])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 400, close)).fetchall()
    if len(q) < 20:
        continue
    qt = [float(x[0]) for x in q]
    fl = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], int(opened - 700), int(close))).fetchall()
    ft = [float(x[0]) for x in fl]
    cy, cn = [0.0], [0.0]
    for x in fl:
        cy.append(cy[-1] + float(x[1]))
        cn.append(cn[-1] + float(x[2]))

    def quote_at(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            return None
        return float(q[i][1]), float(q[i][2])

    def vol(t0, t1):
        a, b = bisect.bisect_right(ft, t0), bisect.bisect_right(ft, t1)
        return cy[b] - cy[a], cn[b] - cn[a]

    RES[w["ticker"]] = w["result"]
    for e in range(60, 841, MOMENT_STEP):
        at = opened + e
        k = int(at) - g0
        if k < 0 or k >= len(g):
            continue
        cq = quote_at(at)
        if cq is None:
            continue
        yb, ya = cq
        for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
            sign = 1 if side == "yes" else -1
            flow = {}
            v = vol(at - 180, at)
            for sec in (30, 60, 180):
                vv = vol(at - sec, at)
                mine, other = vv[0 if side == "yes" else 1], vv[1 if side == "yes" else 0]
                flow[f"flowimb_{sec}s"] = (mine - other) / (mine + other) if mine + other > 0 else 0.0
                pm, po = vol(at - sec - 600, at - sec)
                base = (pm + po) / (600.0 / sec)
                flow[f"flowspike_{sec}s"] = (mine + other) / base if base > 0 else 0.0
            quote = {"ask": ask, "spread": ask - bid, "left_min": (900.0 - e) / 60.0}
            for mn in (1, 2, 3):
                pq = quote_at(at - 60 * mn)
                if pq is not None:
                    quote[f"mom_{mn}m"] = (bid - (pq[0] if side == "yes" else 1 - pq[1])) * 100.0
            f = moment_features(g, sq, k, strike_log, sign, flow, quote)
            if "level_5m" not in f:
                continue
            FEATS.setdefault((w["ticker"], side), []).append((e, ask, bid, f))
print(f"{len(FEATS)} market-sides from {len({k[0] for k in FEATS})} markets since Monday 11-09; the tape ends {hm(max(float(w['close_ts']) for w in wins))} ET")


def best_trip(ticker, side, mom):
    """(net per $1, i_buy, i_sell) best over buy moment i and a LATER SALE j. Settlement is not an option here: buying the side that turned out to win and holding is hindsight about the result, not timing (the 11-11 rule), and it was 71% of the first version's best trips."""
    best = (-9.0, None, None)
    for i, (e, ask, bid, f) in enumerate(mom):
        cost = ask + fee(ask)
        if cost <= 0:
            continue
        for j in range(i + 1, len(mom)):
            xb = mom[j][2]
            if xb <= 0 or xb > SELL_CAP:
                continue
            v = (xb - EXIT_SLIP - fee(xb)) / cost - 1.0
            if v > best[0]:
                best = (v, i, j)
    return best


mk = {}
for (tk, side), mom in FEATS.items():
    if len(mom) < 8:
        continue
    b = best_trip(tk, side, mom)
    if tk not in mk or b[0] > mk[tk][0][0]:
        mk[tk] = (b, side, mom)
B = [(v[0][0], tk, v) for tk, v in mk.items()]
n = len(B)
nets = sorted(x[0] for x in B)
q = lambda p: nets[min(n - 1, int(p * n))]  # noqa: E731
print(f"\nPART 1: the best round trip per market, in hindsight ({n} markets): net per $1 spent")
print(f"positive {sum(1 for x in nets if x > 0) / n:.1%} | above 10% {sum(1 for x in nets if x > 0.10) / n:.1%} | above 25% {sum(1 for x in nets if x > 0.25) / n:.1%} | above 50% {sum(1 for x in nets if x > 0.50) / n:.1%}")
print(f"quartiles {q(0.25):+.2f} / median {q(0.5):+.2f} / {q(0.75):+.2f}; mean {sum(nets) / n:+.3f}")
sold = [x for x in B if x[2][0][2] is not None]
hold = [max(((1.0 if RES[tk] == sd else 0.0) / (m_[1] + fee(m_[1])) - 1.0) for m_ in mm) for (tk, sd), mm in FEATS.items() if len(mm) >= 8]
print(f"for context, the hindsight ceiling of buying the side that won and holding: median {sorted(hold)[len(hold) // 2]:+.2f} per $1 (not a timing trade; left out of everything below)")
for lab, key in (("buy at minute", lambda x: x[2][2][x[2][0][1]][0] / 60.0), ("sell at minute", lambda x: x[2][2][x[2][0][2]][0] / 60.0)):
    xs = sorted(key(x) for x in sold)
    if xs:
        print(f"  among sold: {lab} quartiles {xs[len(xs) // 4]:.1f} / {xs[len(xs) // 2]:.1f} / {xs[3 * len(xs) // 4]:.1f}")
pa = sorted(x[2][2][x[2][0][1]][1] for x in sold)
pb = sorted(x[2][2][x[2][0][2]][2] for x in sold)
if pa:
    print(f"  among sold: buy ask quartiles {pa[len(pa) // 4]:.2f} / {pa[len(pa) // 2]:.2f} / {pa[3 * len(pa) // 4]:.2f}; sell bid quartiles {pb[len(pb) // 4]:.2f} / {pb[len(pb) // 2]:.2f} / {pb[3 * len(pb) // 4]:.2f}")
hl = sorted(x[2][2][x[2][0][2]][0] - x[2][2][x[2][0][1]][0] for x in sold)
if hl:
    print(f"  among sold: seconds held, quartiles {hl[len(hl) // 4]:.0f} / {hl[len(hl) // 2]:.0f} / {hl[3 * len(hl) // 4]:.0f}")

waves = [x for x in sold if x[0] > 0.10]
print(f"\nPART 2: {len(waves)} markets with a best round trip above 10% that sells before settlement")


def rank(mom, idx, name):
    v = mom[idx][3].get(name)
    if v is None:
        return None
    xs = [m[3][name] for m in mom if name in m[3]]
    if len(xs) < 8:
        return None
    return (sum(1 for y in xs if y < v) + 0.5 * sum(1 for y in xs if y == v) - 0.5) / (len(xs) - 1) if len(xs) > 1 else None


names = sorted({k for x in waves for m in x[2][2] for k in m[3]} - {"ask", "left_min"}) + ["ask", "left_min"]
for lab, pick in (("BUY moment", 1), ("SELL moment", 2)):
    rows = []
    for nm in names:
        rs = []
        for x in waves:
            (_, i, j), side, mom = x[2]
            r = rank(mom, i if pick == 1 else j, nm)
            if r is not None:
                rs.append((r, mom[i if pick == 1 else j][3].get("level_13m")))
        if len(rs) < 100:
            continue
        m_ = statistics.fmean(r for r, _ in rs)
        se = statistics.stdev(r for r, _ in rs) / math.sqrt(len(rs))
        if se <= 0:
            continue
        lv = sorted(l for _, l in rs if l is not None)
        parts = []
        if len(lv) > 90:
            c1, c2 = lv[len(lv) // 3], lv[2 * len(lv) // 3]
            for lo_, hi_ in ((-1e9, c1), (c1, c2), (c2, 1e9)):
                sub = [r for r, l in rs if l is not None and lo_ <= l < hi_]
                parts.append(statistics.fmean(sub) if len(sub) > 20 else float("nan"))
        same = bool(parts) and all(p == p for p in parts) and (all(p > 0.5 for p in parts) or all(p < 0.5 for p in parts))
        rows.append((abs((m_ - 0.5) / se), nm, m_, (m_ - 0.5) / se, len(rs), parts, same))
    rows.sort(reverse=True)
    print(f"\n{lab}: each feature's rank inside the market's own readings at the best moment (0.5 = says nothing); {len(rows)} features, {sum(1 for r in rows if r[0] > 2)} past |t| = 2 (about {0.05 * len(rows):.0f} by chance)")
    print("feature        | mean rank (t) | markets | inside level_13m low/mid/high at that moment | same side of 0.5 in all three")
    for _, nm, m_, t_, k_, parts, same in rows[:30]:
        print(f" {nm:13s} | {m_:.3f} ({t_:+.1f}) | {k_:5d} | {' / '.join(f'{p:.3f}' for p in parts)} | {'yes' if same else 'no '}")
print("DONE")
